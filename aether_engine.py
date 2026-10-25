import os
import json
import logging
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from alpaca_executor import AlpacaExecutionClient, BROKER_ERROR_FREEZE_THRESHOLD
import strategy as strat
import us_strategy
import notify
import decision_log
import risk_controller
import cf_access
from heartbeat import beat  # aether_safety package (2026-10-04): external watchdog heartbeat
from zoneinfo import ZoneInfo

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AETHER_CORE")


def _env_float(name: str, default: str) -> float:
    """float(os.getenv(...)) that fails closed to `default` instead of crashing the whole
    process at import time. A malformed .env value (e.g. a trailing blank) used to take down
    uvicorn entirely -- no engine, no dashboard, no Telegram -- for a single bad number."""
    raw = os.getenv(name, default)
    try:
        v = float(raw)
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf guard
            raise ValueError(raw)
        return v
    except (TypeError, ValueError):
        logger.error(f"{name}={raw!r} is not a valid number; using default {default!r}")
        return float(default)


def _env_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.error(f"{name}={raw!r} is not a valid integer; using default {default!r}")
        return int(default)


def _sd_notify(message: str) -> None:
    """Speaks systemd's sd_notify protocol directly (a Unix datagram to $NOTIFY_SOCKET) so no
    extra dependency is needed for just two message types. A no-op whenever NOTIFY_SOCKET isn't
    set (Type=simple services, or running outside systemd entirely, e.g. the test suites).
    READY=1 tells systemd startup actually finished; WATCHDOG=1 is the heartbeat that
    WatchdogSec in aether.service expects on a schedule -- if it ever stops arriving (the whole
    process wedged, not just crashed) systemd kills and restarts us, which nothing did before."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    import socket as _socket
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(message.encode())
        finally:
            sock.close()
    except Exception as e:
        logger.warning(f"sd_notify({message!r}) failed: {type(e).__name__}")


LOCK_FILE = os.getenv("AETHER_LOCK_FILE", os.path.join(os.path.expanduser("~"), ".aether.lock"))
_lock_fd = None


def _acquire_singleton_lock() -> None:
    """Refuses to start a second AETHER process against the same state files. Nothing before
    this stopped two copies running at once -- e.g. a stuck systemd stop during a slow shutdown
    overlapping with Restart=always bringing up a new one, or someone running the engine by hand
    while the service is also up -- and two processes both reading/writing aether_state.json and
    both submitting orders is a real risk, not a theoretical one. Uses a POSIX advisory lock
    (flock) on a dedicated file, held for the life of the process; released automatically when
    the process exits, whether cleanly or not, since the kernel owns the lock, not the file."""
    global _lock_fd
    import fcntl
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.critical(f"Another AETHER process already holds {LOCK_FILE} -- refusing to start "
                        f"a second instance against the same state files.")
        raise SystemExit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
    logger.info(f"Singleton lock acquired ({LOCK_FILE}, pid {os.getpid()}).")


@asynccontextmanager
async def lifespan(_app):
    # First: refuse to start at all if another instance already holds the lock. Must happen
    # before any state is read or any thread started.
    _acquire_singleton_lock()
    # Decide WHY we started before any background thread exists: the monitor thread rewrites the
    # heartbeat file the moment it runs, which would erase the evidence this check reads.
    report = startup_report()
    strategy_thread = None
    if STRATEGY_ENABLED:
        strategy_thread = threading.Thread(target=strategy_loop, daemon=True, name="aether-strategy")
        strategy_thread.start()
    else:
        logger.info("Strategy engine disabled (set STRATEGY_ENABLED=true in .env to enable).")
    us_thread = None
    if US_ENABLED:
        us_thread = threading.Thread(target=us_loop, daemon=True, name="aether-us")
        us_thread.start()
    threading.Thread(target=monitor_loop, daemon=True, name="aether-monitor").start()
    notify.start_command_poller(handle_command)
    logger.info(f"Telegram alerts {'enabled' if notify.enabled() else 'disabled (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set)'}.")
    notify.send(report, key="startup", cooldown=60)
    write_heartbeat()
    _sd_notify("READY=1")
    yield
    _shutting_down.set()
    notify.send("[AETHER] Service stopping (VM shutdown, restart or manual stop).", key="shutdown", cooldown=60)
    STRATEGY_STOP.set()
    US_STOP.set()
    # These are daemon threads: if the interpreter exits while one is mid-cycle (submitting a
    # live order, polling for a fill), it is severed with no cleanup -- the order can end up
    # live at the broker and never recorded locally. Give a cycle already in flight a real
    # chance to finish instead of a bare sleep(2). TimeoutStopSec in aether.service must stay
    # at least this high, or systemd SIGKILLs the process before this join returns.
    JOIN_TIMEOUT = 300
    if strategy_thread is not None:
        strategy_thread.join(timeout=JOIN_TIMEOUT)
        if strategy_thread.is_alive():
            logger.warning(f"Strategy thread did not stop within {JOIN_TIMEOUT}s of shutdown request.")
    if us_thread is not None:
        us_thread.join(timeout=JOIN_TIMEOUT)
        if us_thread.is_alive():
            logger.warning(f"US thread did not stop within {JOIN_TIMEOUT}s of shutdown request.")
    write_heartbeat(clean_stop=True)
    notify.STOP.set()
    if _lock_fd is not None:
        try:
            import fcntl
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception as e:
            logger.warning(f"Could not explicitly release the singleton lock: {e}")


app = FastAPI(title="AETHER Autonomous Trading Engine", version="5.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

broker = AlpacaExecutionClient(paper=True)
# US engine only: a separate LIVE-money connection, used only when US_LIVE=true AND US_EXECUTE=true.
US_LIVE = os.getenv("US_LIVE", "false").lower() == "true"
US_LIVE_MAX_USD = _env_float("US_LIVE_MAX_USD", "0")
US_REQUIRE_APPROVAL = US_LIVE or os.getenv("US_REQUIRE_APPROVAL", "false").lower() == "true"
us_broker = (AlpacaExecutionClient(paper=False, key_env="APCA_LIVE_API_KEY_ID",
                                   secret_env="APCA_LIVE_API_SECRET_KEY") if US_LIVE else broker)


def _check_us_live_caps() -> None:
    """When US_LIVE is on, RISK_MAX_ORDER_USD_LIVE and RISK_MAX_DAILY_BUY_USD_LIVE must each be
    at least US_LIVE_MAX_USD, or risk_controller.preflight() safely refuses every live rebalance
    plan near the cap (no orders sent -- but also no visible reason unless you check the logs).
    This only warns loudly; it doesn't block startup, since the paper engine and dashboard must
    keep working even with a live-only misconfiguration."""
    if not US_LIVE:
        return
    order_cap = risk_controller._env_usd("RISK_MAX_ORDER_USD_LIVE", "100")
    daily_cap = risk_controller._env_usd("RISK_MAX_DAILY_BUY_USD_LIVE", "100")
    if order_cap < US_LIVE_MAX_USD or daily_cap < US_LIVE_MAX_USD:
        msg = (f"US_LIVE_MAX_USD=${US_LIVE_MAX_USD:,.2f} but RISK_MAX_ORDER_USD_LIVE=${order_cap:,.2f} / "
               f"RISK_MAX_DAILY_BUY_USD_LIVE=${daily_cap:,.2f} -- a live rebalance plan will be refused "
               f"by risk pre-flight (safely, no orders sent) instead of executing. Raise both caps to "
               f"at least US_LIVE_MAX_USD in .env.")
        logger.error(msg)
        notify.send(f"[ALERT] AETHER live caps misaligned: {msg}", key="us-live-caps-misaligned", cooldown=24 * 3600)


_check_us_live_caps()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aether_state.json")
STATE_LOCK = threading.Lock()
STARTED_AT = datetime.now(timezone.utc)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Webhook BUY size is a dollar amount (converted to quantity at the alert price), never a raw
# unit count, and any order above MAX_ORDER_PCT of account equity is rejected outright.
TRADE_NOTIONAL_USD = _env_float("TRADE_NOTIONAL_USD", "100")
MAX_ORDER_PCT = _env_float("MAX_ORDER_PCT", "5") / 100

TRACKED_SYMBOLS = {"BTC/USD", "ETH/USD", "SOL/USD"}

MAX_TRADE_HISTORY = 200
MAX_PORTFOLIO_POINTS = 500
PORTFOLIO_SAMPLE_INTERVAL_SECONDS = 30

if not WEBHOOK_SECRET:
    logger.critical(
        "CRITICAL SECURITY WARNING: WEBHOOK_SECRET is not set. "
        "All authenticated endpoints will refuse requests until it is configured."
    )


class TradeAlertPayload(BaseModel):
    symbol: str
    condition: str
    price: float
    resolution: str
    model_score: float = 95.0
    # TradingView cannot send custom HTTP headers, so the webhook secret may travel in the
    # JSON body instead. repr=False keeps it out of any logged/printed model.
    secret: str = Field(default="", repr=False)

    @field_validator("price")
    @classmethod
    def price_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError("price must be positive")
        return v

    @field_validator("symbol")
    @classmethod
    def symbol_must_not_be_blank(cls, v):
        if not v or not v.strip():
            raise ValueError("symbol must not be blank")
        return v


def normalize_symbol(raw_symbol: str):
    """Best-effort normalization of a TradingView-style ticker (e.g. 'BINANCE:BTCUSDT',
    'COINBASE:BTC-USD', or already 'BTC/USD') into Alpaca's 'BASE/USD' format. Returns
    None if the symbol can't be confidently mapped to a tracked pair — callers must
    treat that as a rejection, not a fallback to some default asset."""
    s = raw_symbol.split(":")[-1].upper().replace("-", "").replace("_", "")
    if "/" in s:
        candidate = s
    else:
        candidate = None
        for base in ("BTC", "ETH", "SOL"):
            if s.startswith(base) and s[len(base):] in ("USD", "USDT", "USDC"):
                candidate = f"{base}/USD"
                break
    if candidate in TRACKED_SYMBOLS:
        return candidate
    return None


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def default_us_state() -> dict:
    return {
        "cash": None,
        "holdings": {},
        "last_rebalance_month": None,
        "last_rebalance_at": None,
        "signal_date": None,
        "targets": {},
        "scores": {},
        "selected": [],
        "history": [],
        "equity_history": [],
        "last_snapshot_date": None,
        "last_prices": {},
        "live_proposal": None,
        "frozen": None,
    }


def _us_state_key() -> str:
    """Paper and live portfolios must never share persistent state."""
    return "us_live" if US_LIVE else "us_paper"


def _us_state(state: dict) -> dict:
    return state[_us_state_key()]


def default_state() -> dict:
    return {
        "positions": {sym: {"long": 0.0, "short": 0.0} for sym in TRACKED_SYMBOLS},
        "trade_history": [],
        "portfolio_history": [],
        "operating_state": "Observation (Shadow Recorder)",
        "risk_level": "Medium",
        "loss_used_pct": 0.0,
        "last_signal": None,
        "last_execution_time": "Never",
        "last_webhook_received_at": None,
        "active_grid": {},
        "strategy": {
            "positions": {},
            "day": {},
            "last_signal_bar": {},
            "last_trade_ts": {},
            "last_exit_attempt": {},
        },

        # Completely independent US portfolios.
        "us_paper": default_us_state(),
        "us_live": default_us_state(),
    }


def _fsync_dir(path: str) -> None:
    """Fsyncs the directory containing `path` so a rename into place is durable across a power
    loss too, not just the file's own content. Best-effort: some filesystems don't support
    directory fsync, so failures here are logged, not raised."""
    try:
        dir_fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as e:
        logger.warning(f"Directory fsync failed for {path}: {type(e).__name__}: {e}")


def _quarantine_bad_state_file(path: str) -> None:
    """Moves a corrupt state file aside instead of leaving it in place, where the next
    save_state() would otherwise copy it over the one good backup (see save_state's docstring
    for the exact scenario this prevents). Never raises -- this runs inside an already-degraded
    path and must not itself become a new failure."""
    if not os.path.exists(path):
        return
    try:
        quarantined = f"{path}.corrupt.{int(time.time())}"
        os.replace(path, quarantined)
        logger.error(f"Quarantined corrupt state file: {path} -> {quarantined}")
    except Exception as e:
        logger.error(f"Could not quarantine corrupt state file {path}: {e}")


def _repair_primary_from(good_content: dict) -> None:
    """After recovering from .bak, immediately rewrites the primary with that same good content.
    Without this, the primary stays missing/corrupt until the next save_state() call, during
    which .bak would be overwritten with whatever (if anything) is at the primary path at that
    moment -- closing that window is the actual fix for A4.2."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(good_content, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        _fsync_dir(STATE_FILE)
        logger.error("Primary state file repaired from backup.")
    except Exception as e:
        logger.error(f"Could not repair primary state file from backup: {e}")


def _enter_state_corruption_safe_mode(bad_paths: list) -> None:
    """Both the primary and .bak state files were unreadable. Returning a fresh, empty
    default_state() silently here would wipe every open position and reset the daily-loss
    baseline with nothing to notice -- risk_controller's own budget file deliberately fails
    closed instead for exactly this reason (see risk_controller.py's _live_buys_today). This
    uses the SAME manual halt already used everywhere else (touch ~/AETHER_HALT): blocks new
    entries across both engines, never blocks exits, and does not clear itself -- a human has to
    look at what happened and clear it on purpose."""
    try:
        risk_controller.HALT_FILE.touch(exist_ok=True)
    except Exception as e:
        logger.critical(f"Could not create halt file after state corruption: {e}")
    msg = (f"[ALERT] AETHER state corruption: {bad_paths} unreadable. Halted (new entries "
          f"blocked everywhere; exits still work) instead of silently resetting positions and "
          f"the daily-loss baseline. Restore from backups, then: rm ~/AETHER_HALT")
    logger.critical(msg)
    notify.send(msg, key="state-corruption", cooldown=3600)


def load_state() -> dict:
    """Load persistent state with automatic legacy-US migration.

    If the primary is unreadable but .bak is good: recovers from .bak, quarantines the corrupt
    primary (done in the except branch below), and immediately repairs the primary from that
    good content so the next save_state() doesn't copy a still-corrupt (or missing) primary over
    the one good backup -- that used to destroy the only good copy (A4.2).

    If BOTH are unreadable, this does NOT silently return a fresh, empty default_state(): it
    enters the halt-based safe mode above and alerts, instead of quietly wiping every position
    and the daily-loss baseline (A4.3).
    """
    state = default_state()
    bad_paths = []

    for path in (STATE_FILE, STATE_FILE + ".bak"):
        if not os.path.exists(path):
            continue

        try:
            with open(path, "r") as f:
                loaded = json.load(f)

            state.update(loaded)

            positions = state.get("positions") or {}
            for sym in TRACKED_SYMBOLS:
                positions.setdefault(sym, {"long": 0.0, "short": 0.0})
            state["positions"] = positions

            for k, v in default_state()["strategy"].items():
                state["strategy"].setdefault(k, v)

            # Existing installations stored paper state under "us".
            # Copy it to us_paper once. Never copy it to us_live.
            if "us_paper" not in loaded and isinstance(loaded.get("us"), dict):
                state["us_paper"] = json.loads(json.dumps(loaded["us"]))

            for state_name in ("us_paper", "us_live"):
                if not isinstance(state.get(state_name), dict):
                    state[state_name] = {}

                defaults = default_us_state()
                for k, v in defaults.items():
                    if k not in state[state_name]:
                        state[state_name][k] = json.loads(json.dumps(v))

            if path != STATE_FILE:
                logger.error(
                    f"State file unreadable; recovered from backup {path}."
                )
                _repair_primary_from(loaded)

            return state

        except Exception as e:
            logger.error(f"Failed to read state file {path}: {str(e)}")
            bad_paths.append(path)
            _quarantine_bad_state_file(path)

    if bad_paths:
        _enter_state_corruption_safe_mode(bad_paths)
    return state


def save_state(state: dict):
    """Atomic write: a crash or power loss mid-write can never leave a truncated state file.
    The previous good copy is kept as .bak -- also written atomically (tmp file + fsync +
    rename) rather than shutil.copyfile straight onto the live .bak path, which could leave .bak
    itself truncated if the process died mid-copy. load_state() falls back to .bak if the
    primary is ever unreadable, and repairs the primary immediately when that happens so this
    same copy-step can't overwrite .bak with a still-bad primary on the next save (A4.2)."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(STATE_FILE):
        bak_tmp = STATE_FILE + ".bak.tmp"
        shutil.copyfile(STATE_FILE, bak_tmp)
        with open(bak_tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(bak_tmp, STATE_FILE + ".bak")
    os.replace(tmp, STATE_FILE)
    _fsync_dir(STATE_FILE)


def verify_access(secret: str = "", x_webhook_secret: str = ""):
    """Validates incoming requests against the configured webhook secret.

    Fails CLOSED: if WEBHOOK_SECRET is not configured, every request is refused
    rather than allowed through. Uses a constant-time comparison to avoid leaking
    the secret via response-timing.
    """
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service misconfigured: WEBHOOK_SECRET is not set.",
        )
    token = secret or x_webhook_secret
    if not token or not secrets.compare_digest(token, WEBHOOK_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing security token.",
        )
    return True


def record_trade_history(state: dict, symbol: str, side: str, requested_qty: float,
                          executed_qty: float, price: float, result_status: str, detail):
    """Appends one entry to the trade history log — including rejections and errors, not
    just successful fills, so the dashboard shows the full picture of what AETHER did in
    response to every signal it received."""
    entry = {
        "timestamp": utcnow_str(),
        "symbol": symbol,
        "side": side,
        "requested_qty": requested_qty,
        "executed_qty": executed_qty,
        "price": price,
        "status": result_status,
        "detail": detail,
    }
    history = state.get("trade_history", [])
    history.append(entry)
    state["trade_history"] = history[-MAX_TRADE_HISTORY:]
    return entry


STRATEGY_ENABLED = os.getenv("STRATEGY_ENABLED", "false").lower() == "true"
STRATEGY_INTERVAL = _env_int("STRATEGY_INTERVAL_SECONDS", "60")
STRATEGY_COOLDOWN = _env_int("STRATEGY_COOLDOWN_SECONDS", "7200")
STRATEGY_MAX_DAILY_LOSS = _env_float("STRATEGY_MAX_DAILY_LOSS_PCT", "3") / 100
STRATEGY_SHADOW_EQUITY = _env_float("STRATEGY_SHADOW_EQUITY", "100000")
STRATEGY_PARAMS = strat.Params(risk_pct=float(os.getenv("STRATEGY_RISK_PCT", "1")) / 100)
STRATEGY_BAR_SECONDS = 3600
STRATEGY_MIN_NOTIONAL = 10.0
STRATEGY_EXIT_RETRY_SECONDS = 300
STRATEGY_STOP = threading.Event()
STRATEGY_STATUS = {"last_cycle": None, "last_error": None, "kill_switch": False, "symbols": {}}
BACKTEST_CACHE = {}
_data_client = None


def _mode() -> str:
    return "LIVE" if os.getenv("LIVE_TRADING", "false").lower() == "true" else "SHADOW"


def get_data_client():
    global _data_client
    if _data_client is None:
        from alpaca.data.historical import CryptoHistoricalDataClient
        key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
        _data_client = CryptoHistoricalDataClient(key, secret) if key and secret else CryptoHistoricalDataClient()
    return _data_client


def fetch_bars(symbol: str, hours: int) -> list:
    """Hourly OHLCV bars for roughly the last `hours` hours (oldest first)."""
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame
    end = datetime.now(timezone.utc)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour,
                            start=end - timedelta(hours=hours + 48), end=end)
    raw = get_data_client().get_crypto_bars(req).data.get(symbol, [])
    return [{"t": b.timestamp.isoformat(), "o": float(b.open), "h": float(b.high),
             "l": float(b.low), "c": float(b.close), "v": float(b.volume)} for b in raw]


def split_closed(bars: list, now: datetime):
    """Drops the still-forming last bar so indicators only use completed candles.
    Returns (closed_bars, latest_price)."""
    price = bars[-1]["c"]
    if datetime.fromisoformat(bars[-1]["t"]) + timedelta(seconds=STRATEGY_BAR_SECONDS) > now:
        return bars[:-1], price
    return bars, price


def apply_fill(state: dict, symbol: str, side: str, executed_qty: float):
    pos = state["positions"].setdefault(symbol, {"long": 0.0, "short": 0.0})
    if side == "BUY":
        pos["long"] += executed_qty
    else:
        close_amount = min(executed_qty, pos["long"])
        pos["long"] -= close_amount
        pos["short"] += executed_qty - close_amount


def _strategy_order(state, symbol, side, qty, price, is_closing, available_long, note):
    """Sends one strategy order through the same broker guards and history log as webhooks."""
    result = broker.execute_order(symbol=symbol, qty=qty, side=side, limit_price=price,
                                  is_closing=is_closing, available_long=available_long)
    status_ = result.get("status")
    executed = result.get("qty", 0.0)
    detail = f"strategy:{note}" + (f" ({result.get('reason') or result.get('message') or result.get('order_id')})")
    record_trade_history(state, symbol, side, qty, executed, price, status_, detail)
    decision_log.log("crypto", "order", symbol=symbol, decision=side, reason=note, price=price, mode=_mode(),
                     status=status_, requested_qty=qty, executed_qty=executed, is_closing=is_closing, detail=detail)
    ok = status_ in ("success", "shadow_recorded")
    if ok:
        apply_fill(state, symbol, side, executed)
        state["last_signal"] = f"{symbol} ({side}) [strategy]"
        state["last_execution_time"] = utcnow_str()
        notify.send(f"[TRADE] {_mode()} {side} {executed:g} {symbol} @ ${price:,.2f} (about ${executed * price:,.0f}) - {note}")
    else:
        notify.send(f"[ALERT] Strategy {side} order for {symbol} was {status_}: {detail}",
                    key=f"order-fail:{symbol}:{side}", cooldown=900)
    return ok, executed


def _process_symbol(state: dict, symbol: str, bars: list, equity: float, killed: bool, now: datetime) -> dict:
    p = STRATEGY_PARAMS
    sd = state["strategy"]
    closed, price = split_closed(bars, now)
    ind = strat.compute_indicators(closed, p)
    i = len(closed) - 1
    info = {"symbol": symbol, "price": price, "bars": len(closed), "updated": utcnow_str(),
            "state": "FLAT", "note": ""}
    if i < 1 or not strat.ready(ind, i, p):
        info.update(state="WARMUP", note=f"needs {p.warmup + 1} closed bars, have {len(closed)}")
        return info

    info.update(ema_fast=ind["ema_fast"][i], ema_slow=ind["ema_slow"][i], ema_trend=ind["ema_trend"][i],
                rsi=ind["rsi"][i], atr=ind["atr"][i], trend_ok=closed[i]["c"] > ind["ema_trend"][i])
    pos = sd["positions"].get(symbol)
    bar_t = closed[i]["t"]
    info["bar_t"] = bar_t
    now_ts = now.timestamp()

    if pos:
        pos["stop"] = max(pos["stop"], closed[i]["c"] - p.atr_mult * ind["atr"][i])
        reason = "stop" if price <= pos["stop"] else ("trend_exit" if strat.exit_signal(ind, i, p) else None)
        info.update(state="LONG", stop=pos["stop"], entry=pos["entry"], qty=pos["qty"])
        if reason:
            if now_ts - sd["last_exit_attempt"].get(symbol, 0) < STRATEGY_EXIT_RETRY_SECONDS:
                info["note"] = "exit retry pending"
                return info
            sd["last_exit_attempt"][symbol] = now_ts
            avail = pos["qty"]
            if os.getenv("LIVE_TRADING", "false").lower() == "true":
                broker_qty = broker.get_position_qty(symbol)
                if broker_qty is not None:
                    avail = min(avail, broker_qty)
            if avail <= 0:
                sd["positions"].pop(symbol, None)
                info.update(state="FLAT", note="stale position dropped (broker holds none)")
                return info
            aggressiveness = 0.98 if reason == "stop" else 0.995
            ok, _ = _strategy_order(state, symbol, "SELL", avail, round(price * aggressiveness, 2), True, avail, reason)
            if ok:
                sd["positions"].pop(symbol, None)
                sd["last_trade_ts"][symbol] = now_ts
                info.update(state="FLAT", note=f"exited: {reason}")
            else:
                info["note"] = f"exit failed: {reason}"
        return info

    if strat.entry_signal(ind, i, p) and sd["last_signal_bar"].get(symbol) != bar_t:
        sd["last_signal_bar"][symbol] = bar_t
        stop = price - p.atr_mult * ind["atr"][i]
        qty = round(strat.position_size(equity, price, stop, p), 6)
        if killed:
            info["note"] = "entry signal blocked: daily-loss kill-switch"
        elif now_ts - sd["last_trade_ts"].get(symbol, 0) < STRATEGY_COOLDOWN:
            info["note"] = "entry signal blocked: cooldown"
        elif qty * price < STRATEGY_MIN_NOTIONAL:
            info["note"] = "entry signal skipped: size below minimum"
        else:
            ok, executed = _strategy_order(state, symbol, "BUY", qty, round(price * 1.001, 2), False, 0.0, "ema_cross_entry")
            if ok:
                sd["positions"][symbol] = {"qty": executed, "entry": price, "stop": stop, "t": utcnow_str()}
                sd["last_trade_ts"][symbol] = now_ts
                info.update(state="LONG", stop=stop, entry=price, qty=executed, note="entered")
            else:
                info["note"] = "entry order rejected"
    if info["note"].startswith("entry signal"):
        notify.send(f"[SIGNAL] {symbol}: {info['note']}", key=f"blocked:{symbol}:{bar_t}", cooldown=86400)
    return info


def _crypto_event(note: str) -> str:
    if note == "entered":
        return "entry"
    if note.startswith("exited"):
        return "exit"
    if note.startswith("entry signal blocked"):
        return "blocked"
    if note.startswith("entry signal skipped"):
        return "skipped"
    if note in ("entry order rejected",) or note.startswith("exit failed"):
        return "order_failed"
    return "evaluate"


def _log_crypto_decision(info: dict):
    """Decision log: one row per new closed bar per symbol, plus any change of state or note."""
    note = info.get("note") or ""
    keys = ("ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "trend_ok", "stop", "entry", "qty", "bars")
    decision_log.log_changed(
        f"crypto:{info.get('symbol')}", (info.get("bar_t"), info.get("state"), note),
        engine="crypto", event=_crypto_event(note), mode=_mode(), symbol=info.get("symbol"),
        bar_time=info.get("bar_t"), decision=info.get("state"), reason=note or None, price=info.get("price"),
        **{k: info[k] for k in keys if k in info})


def strategy_cycle(fetch=None):
    fetch = fetch or fetch_bars
    now = datetime.now(timezone.utc)
    balances = broker.get_account_balance()
    equity = float(balances["portfolio_value"]) if balances.get("source") == "live" else STRATEGY_SHADOW_EQUITY

    with STATE_LOCK:
        state = load_state()
        today = now.strftime("%Y-%m-%d")
        day = state["strategy"].get("day") or {}
        if day.get("date") != today:
            day = {"date": today, "start_equity": equity}
            state["strategy"]["day"] = day
            save_state(state)
        killed = equity < day["start_equity"] * (1 - STRATEGY_MAX_DAILY_LOSS)
    was_killed = STRATEGY_STATUS["kill_switch"]
    STRATEGY_STATUS["kill_switch"] = killed
    risk_controller.set_kill("crypto", killed)
    if killed and not was_killed:
        notify.send(f"[ALERT] Daily-loss kill-switch ACTIVE: equity ${equity:,.2f} is more than "
                    f"{STRATEGY_MAX_DAILY_LOSS * 100:g}% below today's start (${day['start_equity']:,.2f}). "
                    f"New entries are blocked until UTC midnight; exits still work.", key=f"kill:{today}", cooldown=86400)
    if killed != was_killed:
        decision_log.log("crypto", "kill_switch", decision="active" if killed else "cleared", mode=_mode(),
                         reason=f"daily loss limit {STRATEGY_MAX_DAILY_LOSS * 100:g}%",
                         equity=equity, start_equity=day["start_equity"])

    for symbol in sorted(TRACKED_SYMBOLS):
        try:
            bars = fetch(symbol, STRATEGY_PARAMS.warmup + 100)
            if not bars:
                raise RuntimeError("no bars returned")
            with STATE_LOCK:
                state = load_state()
                info = _process_symbol(state, symbol, bars, equity, killed, now)
                save_state(state)
            STRATEGY_STATUS["symbols"][symbol] = info
            _log_crypto_decision(info)
        except Exception as e:
            logger.error(f"Strategy error for {symbol}: {str(e)}")
            notify.send(f"[ALERT] Strategy error for {symbol}: {str(e)[:300]}", key=f"err:{symbol}", cooldown=3600)
            decision_log.log_changed(f"crypto:{symbol}", ("error", str(e)[:120]), engine="crypto", event="error",
                                     mode=_mode(), symbol=symbol, reason=str(e)[:300])
            STRATEGY_STATUS["symbols"][symbol] = {"symbol": symbol, "state": "ERROR", "note": str(e),
                                                  "updated": utcnow_str()}


def strategy_loop():
    logger.info(f"Strategy engine started: every {STRATEGY_INTERVAL}s, LIVE_TRADING="
                f"{os.getenv('LIVE_TRADING', 'false')}, params={asdict(STRATEGY_PARAMS)}")
    while not STRATEGY_STOP.is_set():
        try:
            strategy_cycle()
            STRATEGY_STATUS["last_error"] = None
        except Exception as e:
            logger.exception("Strategy cycle failed")
            STRATEGY_STATUS["last_error"] = str(e)
        STRATEGY_STATUS["last_cycle"] = utcnow_str()
        STRATEGY_STATUS["last_cycle_ts"] = time.time()
        STRATEGY_STOP.wait(STRATEGY_INTERVAL)


# --- DASHBOARD LOGIN (cookie session) ---
import time as _time
from urllib.parse import parse_qsl as _parse_qsl, urlencode as _urlencode
from fastapi import Form as _Form, Request as _Request
from fastapi.responses import HTMLResponse as _HTMLResponse, RedirectResponse as _RedirectResponse

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
_SESSIONS = set()
_SESSION_COOKIE = "aether_session"
_OPEN_PATHS = {"/login", "/logout", "/webhook/tradingview"}




_LOGIN_HTML = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AETHER login</title>
<body style="background:#0d1117;color:#c9d1d9;font-family:system-ui;display:flex;height:100vh;align-items:center;justify-content:center;margin:0">
<form method="post" action="/login" style="display:flex;gap:8px">
<input type="password" name="password" placeholder="Password" autofocus style="padding:10px;border-radius:6px;border:1px solid #30363d;background:#161b22;color:#c9d1d9">
<button style="padding:10px 16px;border-radius:6px;border:0;background:#238636;color:#fff">Enter</button>
</form></body>"""


@app.get("/login", response_class=_HTMLResponse)
def _login_page():
    return _LOGIN_HTML


# Per-source login lockout. The old defense was a 1s sleep on a failed attempt -- that only
# slows one connection at a time; nothing stopped many connections from brute-forcing in
# parallel. This tracks failures per client IP and locks that source out for a while after too
# many, with an alert so a real attack doesn't go unnoticed. In-memory only (resets on restart),
# which is fine for what this defends against: it only needs to survive one attack, not forever.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 300
_login_attempts = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _client_ip(request: _Request) -> str:
    # Cloudflare Tunnel sets this on every request it forwards; the SSH-tunnel fallback hits
    # the app directly, where request.client.host is the only thing available (always
    # 127.0.0.1 there, which is fine -- that path is already restricted to whoever can SSH in).
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")


@app.post("/login")
def _login(request: _Request, password: str = _Form(default="")):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD is not set in .env")
    ip = _client_ip(request)
    now = _time.time()
    with _LOGIN_ATTEMPTS_LOCK:
        if len(_login_attempts) > 10000:  # defensive cap: don't let this dict grow unbounded
            _login_attempts.clear()
        rec = _login_attempts.get(ip)
        if rec and rec["locked_until"] > now:
            raise HTTPException(status_code=429,
                                detail=f"Too many failed attempts. Try again in {int(rec['locked_until'] - now)}s.")
    if not secrets.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode()):
        _time.sleep(1)
        with _LOGIN_ATTEMPTS_LOCK:
            rec = _login_attempts.setdefault(ip, {"count": 0, "locked_until": 0.0})
            rec["count"] += 1
            if rec["count"] >= _LOGIN_MAX_ATTEMPTS:
                rec["locked_until"] = now + _LOGIN_LOCKOUT_SECONDS
                rec["count"] = 0
                notify.send(f"[ALERT] Dashboard login locked out for {ip} after "
                            f"{_LOGIN_MAX_ATTEMPTS} failed attempts in a row.",
                            key=f"login-lockout:{ip}", cooldown=3600)
        raise HTTPException(status_code=401, detail="Wrong password")
    with _LOGIN_ATTEMPTS_LOCK:
        _login_attempts.pop(ip, None)
    sid = _session_token()
    resp = _RedirectResponse("/", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, sid, httponly=True, secure=True, samesite="lax", max_age=_SESSION_DAYS * 86400)
    return resp


@app.post("/logout")
def _logout(request: _Request):
    _rotate_session_key()
    resp = _RedirectResponse("/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


import hmac as _hmac
import hashlib as _hashlib
import os as _os
import pathlib as _pathlib

_SESSION_DAYS = 7
_SESSION_KEY_FILE = _pathlib.Path.home() / ".aether_session_key"


def _rotate_session_key():
    """New signing key: every existing login cookie stops working."""
    key = secrets.token_bytes(32)
    tmp = _SESSION_KEY_FILE.with_name(_SESSION_KEY_FILE.name + ".tmp")
    tmp.write_bytes(key)
    _os.chmod(tmp, 0o600)
    tmp.replace(_SESSION_KEY_FILE)
    return key


def _session_key():
    try:
        key = _SESSION_KEY_FILE.read_bytes()
        if len(key) >= 32:
            return key
    except FileNotFoundError:
        pass
    return _rotate_session_key()


def _session_token():
    expires = str(int(_time.time()) + _SESSION_DAYS * 86400)
    sig = _hmac.new(_session_key(), expires.encode(), _hashlib.sha256).hexdigest()
    return expires + "." + sig


def _session_ok(token):
    try:
        expires, sig = token.split(".", 1)
        if int(expires) < _time.time():
            return False
        good = _hmac.new(_session_key(), expires.encode(), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(sig, good)
    except Exception:
        return False


from starlette.responses import JSONResponse as _JSONResponse

_PUBLIC_PATHS = {"/login", "/webhook/tradingview"}
# Hosts the app treats as "reached directly, not through the Cloudflare Tunnel" -- the
# documented SSH-tunnel fallback (ssh -N -L 8000:127.0.0.1:8000 then http://127.0.0.1:8000)
# connects straight to the loopback address, bypassing Cloudflare Access by design. Traffic that
# arrives with any OTHER Host header reached the app through some hostname/routing path, which
# must mean Cloudflare Access (if the app's own DNS/tunnel config is correct) -- so that's
# exactly the traffic the Access JWT check below applies to.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@app.middleware("http")
async def _dashboard_auth(request: _Request, call_next):
    """Every path except the TradingView webhook needs a dashboard session. Traffic that did not
    arrive via the loopback fallback also needs a valid Cloudflare Access JWT, once
    CF_ACCESS_TEAM_DOMAIN/CF_ACCESS_AUD are configured (see cf_access.py) -- an in-app check that
    a request actually passed through Access, independent of the edge policy. A second hostname
    or CNAME pointed at this origin, or a too-wide Access path pattern, would show up here as a
    request with no valid JWT and get refused instead of silently reaching /login or /api/*.
    Fails closed: unknown paths (including /docs and /openapi.json) are refused too."""
    path = request.url.path
    if path == "/webhook/tradingview":
        return await call_next(request)
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    cf_login = False
    if host not in _LOOPBACK_HOSTS:
        if not cf_access.verify(request.headers.get("cf-access-jwt-assertion", "")):
            return _JSONResponse({"detail": "Access verification failed."}, status_code=401)
        # Password-free mode: a valid Access JWT means the request already passed the
        # "Only Me" email login at Cloudflare, so that IS the dashboard login. Only when
        # Access verification is configured -- verify() returns True when it is not.
        cf_login = cf_access.configured()
    sid = request.cookies.get(_SESSION_COOKIE, "")
    if path == "/login" or cf_login or (sid and _session_ok(sid)):
        return await call_next(request)
    if request.method == "GET" and path == "/":
        return _RedirectResponse("/login", status_code=303)
    return _JSONResponse({"detail": "Not logged in."}, status_code=401)


@app.get("/api/strategy")
async def get_strategy_status():
    """Live strategy engine status: per-symbol indicators, open strategy positions, guards."""
    pass  # auth: _dashboard_auth session middleware
    with STATE_LOCK:
        state = load_state()
    return {
        "status": "success",
        "enabled": STRATEGY_ENABLED,
        "mode": "LIVE" if os.getenv("LIVE_TRADING", "false").lower() == "true" else "SHADOW",
        "params": asdict(STRATEGY_PARAMS),
        "interval_seconds": STRATEGY_INTERVAL,
        "cooldown_seconds": STRATEGY_COOLDOWN,
        "max_daily_loss_pct": STRATEGY_MAX_DAILY_LOSS * 100,
        "kill_switch": STRATEGY_STATUS["kill_switch"],
        "last_cycle": STRATEGY_STATUS["last_cycle"],
        "last_error": STRATEGY_STATUS["last_error"],
        "symbols": [STRATEGY_STATUS["symbols"][k] for k in sorted(STRATEGY_STATUS["symbols"])],
        "positions": state["strategy"].get("positions", {}),
        "day": state["strategy"].get("day", {}),
    }


@app.get("/api/backtest")
def run_backtest(symbol: str = Query(default="BTC/USD"), days: int = Query(default=365)):
    """Backtests the exact live strategy rules on Alpaca's historical hourly bars.
    Sync endpoint on purpose: FastAPI runs it in a worker thread so it can't block the server."""
    pass  # auth: _dashboard_auth session middleware
    if symbol not in TRACKED_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"Symbol must be one of {sorted(TRACKED_SYMBOLS)}.")
    days = max(90, min(days, 1460))
    key = (symbol, days)
    cached = BACKTEST_CACHE.get(key)
    if cached and datetime.now(timezone.utc).timestamp() - cached[0] < 600:
        return cached[1]
    try:
        bars = fetch_bars(symbol, days * 24)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch historical data: {str(e)}")
    result = strat.backtest(bars, STRATEGY_PARAMS)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    result.update(symbol=symbol, days=days, params=asdict(STRATEGY_PARAMS))
    BACKTEST_CACHE[key] = (datetime.now(timezone.utc).timestamp(), result)
    return result


DIGEST_HOUR_UTC = _env_int("DIGEST_HOUR_UTC", "6")
# Either name works (watchdog.py reads the same pair); HEALTHCHECK_URL is the one test plan RD4 checks.
HEALTHCHECK_PING_URL = os.getenv("HEALTHCHECK_PING_URL", "") or os.getenv("HEALTHCHECK_URL", "")
MONITOR_INTERVAL = 60
_monitor = {"alpaca_fail_streak": 0, "alpaca_alerted": False, "digest_date": None}


def build_status_text(header: str = "AETHER status") -> str:
    now = datetime.now(timezone.utc)
    m = notify.vm_metrics()
    balances = broker.get_account_balance()
    engine_up = notify.fmt_duration((now - STARTED_AT).total_seconds())
    lines = [f"{header} ({_mode()})",
             f"Engine up {engine_up}" + (f" | VM up {notify.fmt_duration(m['vm_uptime_s'])}" if "vm_uptime_s" in m else "")]
    vm = []
    if "load1" in m:
        vm.append(f"load {m['load1']:.2f}/{m['load5']:.2f}/{m['load15']:.2f} ({m['cpus']} cpu)")
    if "mem_used_pct" in m:
        vm.append(f"mem {m['mem_used_pct']:g}%")
    if "disk_used_pct" in m:
        vm.append(f"disk {m['disk_used_pct']:g}% ({m['disk_free_gb']:g} GB free)")
    if vm:
        lines.append("VM: " + " | ".join(vm))
    if m.get("reboot_required"):
        lines.append("VM: restart required (pending updates)")
    svc = service_states()
    if svc:
        lines.append("Services: " + ", ".join(f"{u.split('|')[0]} {st}" for u, st in svc.items()))
    if balances.get("source") == "live":
        lines.append(f"Alpaca: live | equity ${float(balances['portfolio_value']):,.2f} | cash ${float(balances['cash']):,.2f}")
    else:
        lines.append(f"Alpaca: {balances.get('source')} (no live balance)")
    if STRATEGY_ENABLED:
        last = STRATEGY_STATUS.get("last_cycle_ts")
        age = f"{int(time.time() - last)}s ago" if last else "pending"
        lines.append(f"Strategy: ACTIVE | last cycle {age} | kill-switch {'ON' if STRATEGY_STATUS['kill_switch'] else 'off'}")
        for sym in sorted(STRATEGY_STATUS["symbols"]):
            s = STRATEGY_STATUS["symbols"][sym]
            extra = ""
            if s.get("rsi") is not None:
                extra = f" RSI {s['rsi']:.0f} trend {'UP' if s.get('trend_ok') else 'DOWN'}"
            lines.append(f"  {sym} {s['state']}" + (f" ${s['price']:,.2f}" if s.get("price") else "") + extra)
        if STRATEGY_STATUS.get("last_error"):
            lines.append(f"Strategy error: {STRATEGY_STATUS['last_error']}")
    else:
        lines.append("Strategy: disabled")
    with STATE_LOCK:
        state = load_state()
    if US_ENABLED:
        us = _us_state(state)
        held = ", ".join(f"{x} {w * 100:.0f}%" for x, w in sorted(us["targets"].items())) or "not invested yet"
        eq = ""
        if us["cash"] is not None:
            eq = f" | equity ${us['cash'] + sum(q * us['last_prices'].get(x, 0.0) for x, q in us['holdings'].items()):,.2f}"
        lines.append(f"US ETF engine: {_us_mode()} | {held}{eq} | last rebalance {us['last_rebalance_month'] or 'never'}")
    hist = state.get("trade_history", [])
    if hist:
        h = hist[-1]
        lines.append(f"Last trade log: {h['timestamp']} {h['side']} {h['symbol']} {h['status']}")
    return "\n".join(lines)


def build_positions_text() -> str:
    lines = ["Positions"]
    broker_positions = broker.get_open_positions()
    if broker_positions:
        for p in broker_positions:
            lines.append(f"Alpaca: {p.symbol} {p.qty} @ ${float(p.avg_entry_price):,.2f} (P&L ${float(p.unrealized_pl):,.2f})")
    else:
        lines.append("Alpaca: no open positions")
    with STATE_LOCK:
        strat_pos = load_state()["strategy"].get("positions", {})
    for sym, p in sorted(strat_pos.items()):
        lines.append(f"Strategy: {sym} {p['qty']:g} @ ${p['entry']:,.2f}, stop ${p['stop']:,.2f}")
    if not strat_pos:
        lines.append("Strategy: flat")
    return "\n".join(lines)


def handle_command(cmd: str):
    if cmd.startswith("/approve_") or cmd.startswith("/reject_"):
        return _us_live_decide(cmd)
    if cmd == "/status":
        return build_status_text()
    if cmd == "/positions":
        return build_positions_text()
    if cmd in ("/help", "/start"):
        return "AETHER commands:\n/status - VM, account and strategy report\n/positions - open positions\n/help - this list"
    return "Unknown command. Send /help."


def _ping_healthcheck():
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(HEALTHCHECK_PING_URL, headers={"User-Agent": "AETHER-ops/1.0 (+https://ops.ratelark.app)"}), timeout=10).read()
    except Exception as e:
        logger.warning(f"Healthcheck ping failed: {type(e).__name__}")


HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aether_heartbeat.json")
MONITOR_SERVICES = [u.strip() for u in os.getenv("MONITOR_SERVICES", "ssh|ssh.socket,aether-health.timer").split(",") if u.strip()]
_heartbeat_lock = threading.Lock()
_shutting_down = threading.Event()
_service_streak = {}
_services_alerted = set()


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except Exception:
        return None


def read_heartbeat():
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def write_heartbeat(clean_stop: bool = False):
    # Lock: startup, shutdown and the monitor thread all write this file, and concurrent
    # writers sharing one temp file made os.replace fail with FileNotFoundError.
    # Once shutdown has begun only the clean-stop write is allowed: otherwise a monitor
    # cycle waking inside the shutdown window overwrites the marker and the next start
    # is misreported as a crash.
    if _shutting_down.is_set() and not clean_stop:
        return
    try:
        with _heartbeat_lock:
            tmp = HEARTBEAT_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"boot_id": _boot_id(), "ts": time.time(), "clean_stop": clean_stop}, f)
            os.replace(tmp, HEARTBEAT_FILE)
    except Exception as e:
        logger.warning(f"Could not write heartbeat: {type(e).__name__}")


def startup_report() -> str:
    """Says WHY AETHER just started: first run, clean restart, unexpected restart, or the whole
    VM having been rebooted / powered off and back on (boot id changed)."""
    prev = read_heartbeat()
    tail = f"({_mode()} mode, strategy {'on' if STRATEGY_ENABLED else 'off'}). Send /status for a report."
    if prev:
        logger.info(f"Startup check: previous heartbeat clean_stop={prev.get('clean_stop')}, "
                    f"age={time.time() - prev['ts']:.0f}s, same_boot={prev.get('boot_id') == _boot_id()}")
    else:
        logger.info("Startup check: no previous heartbeat file (first start).")
    if not prev:
        return f"[AETHER] First start {tail}"
    gap = notify.fmt_duration(time.time() - prev["ts"])
    seen = datetime.fromtimestamp(prev["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    boot = _boot_id()
    if boot and prev.get("boot_id") and boot != prev["boot_id"]:
        return (f"[ALERT] The VM was rebooted or powered off and back on. AETHER restarted by itself. "
                f"Last seen alive {seen} ({gap} ago). {tail}")
    if prev.get("clean_stop"):
        return f"[AETHER] Service restarted cleanly (was down about {gap}). {tail}"
    return (f"[ALERT] AETHER restarted unexpectedly (crash, hang or watchdog restart). "
            f"Last seen alive {seen}, down about {gap}. {tail}")


# Type=notify means $NOTIFY_SOCKET is set in our own environment so sd_notify() can reach
# systemd. subprocess.run() inherits the environment by default, and `systemctl` itself turns
# out to probe that socket when it sees it set -- harmless (systemd just rejects the message,
# since it isn't from our main PID), but it spams a "Got notification message ... rejected" log
# line every monitor cycle. Stripping it from the child's environment stops that at the source.
_SUBPROCESS_ENV = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}


def service_states() -> dict:
    """systemd state of each entry in MONITOR_SERVICES ({} when systemd is unavailable).
    An entry may list alternatives with '|' (e.g. 'ssh|ssh.socket'); it counts as active if
    any alternative is active, which matters for socket-activated services."""
    out = {}
    for spec in MONITOR_SERVICES:
        states = []
        for unit in spec.split("|"):
            try:
                r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                                   timeout=5, env=_SUBPROCESS_ENV)
                states.append(r.stdout.strip() or "unknown")
            except FileNotFoundError:
                return {}
            except Exception:
                states.append("unknown")
        out[spec] = "active" if "active" in states else next((x for x in states if x != "unknown"), "unknown")
    return out


def check_services():
    """Alerts when a monitored service is not active for 2 consecutive checks (so a normal
    restart blip does not page you), and again when it recovers."""
    for unit, state in service_states().items():
        unit = unit.split("|")[0]
        if state == "unknown":
            continue
        if state == "active":
            _service_streak[unit] = 0
            if unit in _services_alerted:
                _services_alerted.discard(unit)
                notify.send(f"[OK] Service {unit} is active again.")
            continue
        _service_streak[unit] = _service_streak.get(unit, 0) + 1
        if _service_streak[unit] >= 2 and unit not in _services_alerted:
            _services_alerted.add(unit)
            notify.send(f"[ALERT] Service {unit} is {state} on the VM.", key=f"svc:{unit}", cooldown=3600)


_BACKUPS_DIR = os.path.join(os.path.expanduser("~"), "backups")


def _last_backup_age_hours(now: datetime) -> float:
    """Age of the newest local backup archive in hours, or None if there isn't one yet.
    Mirrors dashboard_v2.py's own /api/safety check (same glob, same directory) so the two
    never disagree about what "last backup" means."""
    import glob
    archives = sorted(glob.glob(os.path.join(_BACKUPS_DIR, "aether-*.tar.gz")))
    if not archives:
        return None
    return (now.timestamp() - os.path.getmtime(archives[-1])) / 3600


def monitor_cycle(now: datetime = None):
    now = now or datetime.now(timezone.utc)
    write_heartbeat()
    check_services()
    # The nightly backup alerts on its OWN failure (backup_alert.py), but nothing previously
    # caught the backup not running at all -- the timer silently disabled, the script crashing
    # before it could even call backup_alert.py, etc. This actively checks staleness instead of
    # only showing it passively on the dashboard for whenever someone happens to look.
    age = _last_backup_age_hours(now)
    if age is not None and age > 26:
        notify.send(f"[ALERT] No successful AETHER backup in {age:.0f}h (last one is that old). "
                    f"Check: systemctl status aether-backup.timer, journalctl -u aether-backup.",
                    key="backup-stale", cooldown=6 * 3600)
    m = notify.vm_metrics()
    if m.get("disk_used_pct", 0) >= 85:
        notify.send(f"[ALERT] VM disk is {m['disk_used_pct']:g}% full ({m['disk_free_gb']:g} GB free).", key="disk", cooldown=6 * 3600)
    if m.get("mem_used_pct", 0) >= 90:
        notify.send(f"[ALERT] VM memory is {m['mem_used_pct']:g}% used.", key="mem", cooldown=3600)
    if m.get("load5") is not None and m["load5"] > m["cpus"] * 2:
        notify.send(f"[ALERT] VM load is high: {m['load5']:.2f} (5 min) on {m['cpus']} cpu.", key="load", cooldown=3600)
    if m.get("reboot_required"):
        notify.send("[INFO] The VM has pending updates and needs a restart. Restart it when convenient: sudo reboot.", key="reboot", cooldown=7 * 86400)

    if broker.client is not None:
        src = broker.get_account_balance().get("source")
        if src == "error":
            _monitor["alpaca_fail_streak"] += 1
            if _monitor["alpaca_fail_streak"] >= 3 and not _monitor["alpaca_alerted"]:
                _monitor["alpaca_alerted"] = True
                notify.send("[ALERT] Alpaca API has been unreachable or rejecting requests for 3+ minutes.")
        elif src == "live":
            if _monitor["alpaca_alerted"]:
                notify.send("[OK] Alpaca connection restored.")
            _monitor["alpaca_fail_streak"] = 0
            _monitor["alpaca_alerted"] = False

    stalled = False
    if STRATEGY_ENABLED:
        last = STRATEGY_STATUS.get("last_cycle_ts")
        limit = max(300, STRATEGY_INTERVAL * 5)
        if last and time.time() - last > limit:
            stalled = True
            notify.send(f"[ALERT] Strategy loop has not completed a cycle in {int(time.time() - last)}s.", key="stall", cooldown=3600)

    # The US loop used to have no stall check at all: with STRATEGY_ENABLED=false (a plausible
    # US-only setup) the heartbeat and healthcheck ping kept going even if the US thread had
    # died or hung, because nothing ever looked at US_STATUS["last_check_ts"].
    if US_ENABLED:
        last = US_STATUS.get("last_check_ts")
        limit = max(300, US_CHECK_INTERVAL * 5)
        if last and time.time() - last > limit:
            stalled = True
            notify.send(f"[ALERT] US ETF loop has not completed a cycle in {int(time.time() - last)}s.", key="us-stall", cooldown=3600)

    if HEALTHCHECK_PING_URL and not stalled:
        _ping_healthcheck()

    today = now.strftime("%Y-%m-%d")
    if now.hour == DIGEST_HOUR_UTC and _monitor["digest_date"] != today:
        _monitor["digest_date"] = today
        notify.send(build_status_text("AETHER daily digest"))

    # Reaching here means this cycle did not hang: VM metrics, service checks, and the broker
    # balance check (now timeout-bounded) all returned. A genuinely wedged process -- e.g. a
    # thread stuck holding STATE_LOCK -- would stop this from running at all, so the ping
    # would stop too, and systemd'"'"'s WatchdogSec would eventually restart the whole thing.
    _sd_notify("WATCHDOG=1")
    beat("engine")  # aether_safety's external watchdog checks this independently of systemd's


def monitor_loop():
    while not notify.STOP.is_set() and not _shutting_down.is_set():
        try:
            monitor_cycle()
        except Exception:
            logger.exception("Monitor cycle failed")
        notify.STOP.wait(MONITOR_INTERVAL)


US_ENABLED = os.getenv("US_ENABLED", "false").lower() == "true"
US_EXECUTE = os.getenv("US_EXECUTE", "false").lower() == "true"       # false = simulated (shadow); true = orders on the Alpaca PAPER account
US_ALLOCATION_USD = _env_float("US_ALLOCATION_USD", "5000")
US_CHECK_INTERVAL = _env_int("US_CHECK_INTERVAL_SECONDS", "300")
US_EXEC_AFTER_ET = (15, 30)
US_SLIPPAGE = 0.0005
US_RETRY_SECONDS = 900
# US kill switch. The US engine is the only one that can spend real money, so it gets its own
# loss limits, evaluated every cycle from the equity history. The drawdown default matches the
# retirement level already written down in strategy.md section 8 (US momentum: -13.5%
# out-of-sample worst -> retire at -27%), turning that manual monthly rule into an automatic one.
US_MAX_DAILY_LOSS = _env_float("US_MAX_DAILY_LOSS_PCT", "3") / 100
US_MAX_DRAWDOWN = _env_float("US_MAX_DRAWDOWN_PCT", "27") / 100
US_MAX_PRICE_DEV = _env_float("US_MAX_PRICE_DEV_PCT", "10") / 100
NY = ZoneInfo("America/New_York")
UAE_TZ = timezone(timedelta(hours=4))
US_STOP = threading.Event()
US_LOCK = threading.Lock()
US_STATUS = {"last_check": None, "last_check_ts": None, "last_error": None, "note": "", "preview": None,
             "preview_at": 0.0, "retry_after": 0.0, "kill_switch": None, "kill_reason": None}
_stock_client = None


def _us_mode() -> str:
    return ("LIVE" if US_LIVE else "PAPER") if US_EXECUTE else "SHADOW"


def uae_str(dt: datetime = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(UAE_TZ).strftime("%Y-%m-%d %H:%M UAE")


def get_stock_client():
    global _stock_client
    if _stock_client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        _stock_client = StockHistoricalDataClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"))
    return _stock_client


def fetch_us_daily(symbols, days: int = 450) -> dict:
    """Dividend- and split-adjusted daily closes: {symbol: [{'d': 'YYYY-MM-DD', 'c': close}, ...]}."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment
    end = datetime.now(timezone.utc) - timedelta(minutes=20)      # free plan: historical SIP data is 15 minutes delayed
    req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day,
                           start=end - timedelta(days=days), end=end, adjustment=Adjustment.ALL)
    data = get_stock_client().get_stock_bars(req).data
    return {sym: [{"d": b.timestamp.astimezone(NY).strftime("%Y-%m-%d"), "c": float(b.close)} for b in data.get(sym, [])]
            for sym in symbols}


def fetch_us_prices(symbols) -> dict:
    from alpaca.data.requests import StockLatestTradeRequest
    from alpaca.data.enums import DataFeed
    res = get_stock_client().get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
    return {sym: float(t.price) for sym, t in res.items()}


def us_market_state():
    """(is_open, now_et). Alpaca's clock knows holidays; without it the market is treated as closed."""
    clk = broker.get_clock()
    if clk is not None:
        return bool(clk.is_open), clk.timestamp.astimezone(NY)
    now = datetime.now(timezone.utc).astimezone(NY)
    logger.warning("Broker clock unavailable: treating the US market as closed (no trading on a guessed calendar).")
    return False, now


def us_signals(fetch_daily, today_str: str) -> dict:
    """Target weights from closes up to the last COMPLETED month-end, with a freshness check."""
    daily = fetch_daily(us_strategy.UNIVERSE)
    closes, last_dates = {}, []
    for sym in us_strategy.UNIVERSE:
        bars = us_strategy.bars_before_month(daily.get(sym, []), today_str)
        us_strategy.validate_bars(sym, bars)
        closes[sym] = [b["c"] for b in bars]
        if bars:
            last_dates.append(bars[-1]["d"])
    weights, scores, selected = us_strategy.target_weights(closes)
    signal_date = min(last_dates)
    month_end = datetime.strptime(today_str[:7] + "-01", "%Y-%m-%d") - timedelta(days=1)
    prev_month = month_end.strftime("%Y-%m")
    if signal_date[:7] != prev_month:
        raise ValueError(f"stale data: newest completed-month bar is {signal_date}, expected a date in {prev_month}")
    lag_days = (month_end - datetime.strptime(signal_date, "%Y-%m-%d")).days
    if lag_days > us_strategy.MAX_STALENESS_DAYS:
        raise ValueError(f"stale data: newest completed-month bar is {signal_date}, {lag_days} days before "
                         f"month-end {month_end:%Y-%m-%d} (limit {us_strategy.MAX_STALENESS_DAYS})")
    latest_close = {}
    for sym in us_strategy.UNIVERSE:
        prior = [b for b in daily.get(sym, []) if b["d"] < today_str]  # today's partial bar is not a close
        if prior:
            latest_close[sym] = prior[-1]["c"]
    return {"weights": weights, "scores": scores, "selected": selected, "signal_date": signal_date,
            "latest_close": latest_close}


def _us_holdings(us: dict):
    """Holdings of the strategy ETFs: the broker's truth in paper mode, the simulated book in shadow mode."""
    if US_EXECUTE:
        pos = us_broker.get_positions_map()
        if pos is None:
            return None
        return {s: q for s, q in pos.items() if s in us_strategy.UNIVERSE and q > 1e-9}
    return {s: q for s, q in us["holdings"].items() if q > 1e-9}


def _us_execute_shadow(orders, prices, holdings, cash):
    holdings, results = dict(holdings), []
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "SELL":
            fill = prices[sym] * (1 - US_SLIPPAGE)
            qty = o["qty"]
            cash += qty * fill
            holdings[sym] = holdings.get(sym, 0.0) - qty
            if holdings[sym] <= 1e-9:
                holdings.pop(sym, None)
        else:
            fill = prices[sym] * (1 + US_SLIPPAGE)
            qty = o["notional"] / fill
            cash -= o["notional"]
            holdings[sym] = holdings.get(sym, 0.0) + qty
        results.append({**o, "state": "simulated", "filled_qty": qty, "avg_price": fill})
    return results, holdings, cash


_US_FINAL_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _us_execute_paper(orders, prices, cash, holdings_before=None, mkey="", attempt=0):
    """Sends orders and books only broker-confirmed fills. An order still open at the poll timeout is
    cancelled and re-read, so a late fill cannot leave cash stale. An order that still cannot be
    reconciled is marked 'unreconciled' (the engine then freezes). If positions cannot be read after
    the fills, the confirmed fills are booked onto the known holdings and the rebalance stays incomplete.

    Each order gets a deterministic client_order_id (one per symbol+side+rebalance-month), so a
    submit that times out client-side but actually landed at the broker is recovered instead of
    duplicated on a later retry. For real-money BUYs, any part of the daily budget that
    risk_controller.check() reserved but wasn't actually spent -- an order that errored after the
    check passed, or filled for less than requested -- is released back so it isn't double-counted
    for the rest of the day."""
    results = []
    book = dict(holdings_before or {})
    sell_failed = False  # a SELL that did not fully fill: the BUYs were sized on its proceeds
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "BUY" and sell_failed:
            results.append({**o, "state": "skipped_after_failed_sell"})
            continue
        coid = f"us-{mkey}-{sym}-{o['side']}-{attempt}"[:48] if mkey else None   # one id per ATTEMPT
        if o["side"] == "SELL":
            r = us_broker.submit_market_order(sym, "SELL", qty=o["qty"], client_order_id=coid)
        else:
            r = us_broker.submit_market_order(sym, "BUY", notional=o["notional"], client_order_id=coid)
        if r.get("status") != "submitted":
            results.append({**o, "state": "error", "detail": r.get("message")})
            if o["side"] == "SELL":
                sell_failed = True
            # Return the reserved real-money budget only for a definite refusal. "risk:" never
            # reserved any; an outcome_unknown submit may already have spent it.
            if (o["side"] == "BUY" and not us_broker.paper and not r.get("outcome_unknown")
                    and not str(r.get("message", "")).startswith("risk:")):
                risk_controller.release_live_buy(o["notional"])
            continue
        f = us_broker.wait_for_fill(r["order_id"])
        if f["state"] not in _US_FINAL_STATES:
            us_broker.cancel_order(r["order_id"])
            f = us_broker.wait_for_fill(r["order_id"], timeout=60.0)
        qty = float(f.get("filled_qty") or 0.0)
        px = f["avg_price"] or prices[sym]
        cash += qty * px if o["side"] == "SELL" else -qty * px
        if o["side"] == "BUY" and not us_broker.paper:
            actual = qty * px
            if actual < o["notional"]:
                risk_controller.release_live_buy(o["notional"] - actual)
        book[sym] = book.get(sym, 0.0) + (qty if o["side"] == "BUY" else -qty)
        state = f["state"] if f["state"] in _US_FINAL_STATES else "unreconciled"
        if o["side"] == "SELL" and state != "filled":
            sell_failed = True
        results.append({**o, "state": state, "filled_qty": qty, "avg_price": px, "order_id": r["order_id"]})
        if state == "unreconciled":
            for rest in orders[orders.index(o) + 1:]:
                results.append({**rest, "state": "skipped_after_unreconciled"})
            break
    book = {s: q for s, q in book.items() if q > 1e-9}
    pos = us_broker.get_positions_map()
    if pos is None:
        results.append({"symbol": "*", "side": "CHECK", "qty": 0.0, "notional": 0.0, "state": "positions_unreadable"})
        return results, book, cash
    return results, {s: q for s, q in pos.items() if s in us_strategy.UNIVERSE and q > 1e-9}, cash


def _us_broker_cash():
    """The paper account's cash as a float, or None if the broker did not give a usable number."""
    bal = us_broker.get_account_balance() or {}
    try:
        v = float(bal["cash"]) if bal.get("source") == "live" else float("nan")
    except (KeyError, TypeError, ValueError):
        v = float("nan")
    return None if (v != v or abs(v) == float("inf")) else v


def _us_reconcile_cash(us: dict):
    """PAPER mode runs a sleeve of a much larger paper account and keeps the sleeve's cash in its state file, saved
    only at the end of a rotation. A crash in the middle therefore leaves that number behind the account. Each save
    also records the account cash (`broker_cash_at_save`); here the sleeve cash is corrected, in memory, by how much
    the account cash has moved since, and the holdings are refreshed from the broker so the two stay consistent.

    Returns None when fine, or a short reason the engine must pause: the account cash is unreadable, or it moved by
    more than max($100, half the allocation) outside the engine (for example the paper account was reset)."""
    if not US_EXECUTE or US_LIVE or us.get("cash") is None:
        return None
    now = _us_broker_cash()
    if now is None:
        return "the paper account cash could not be read"
    base = us.get("broker_cash_at_save")
    if base is None:
        us["broker_cash_at_save"] = now      # state saved before this check existed: trust it once, and SAVE that
        try:                                 # now, so even the first rotation after the upgrade is crash-safe
            with STATE_LOCK:
                st = load_state()
                _us_state(st)["broker_cash_at_save"] = now
                save_state(st)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not save the paper cash baseline: {type(e).__name__}: {str(e)[:120]}")
    else:
        delta = now - float(base)
        limit = max(100.0, 0.5 * US_ALLOCATION_USD)
        if abs(delta) > limit:
            return f"the paper account cash moved ${delta:,.2f} outside the engine (limit ${limit:,.0f})"
        us["cash"] = float(us["cash"]) + delta
        us["broker_cash_at_save"] = now
    held = _us_holdings(us)
    if held is not None:
        us["holdings"] = held
    return None


def _us_rebalance(us: dict, today: str, mkey: str, now_et: datetime, fetch_daily, fetch_prices):
    if us.get("frozen"):
        US_STATUS["note"] = f"US engine frozen: {us['frozen']}"
        notify.send(f"[ALERT] US ETF engine is frozen and will not trade: {us['frozen']}",
                    key="us-frozen", cooldown=6 * 3600)
        return
    if time.time() < US_STATUS["retry_after"]:
        US_STATUS["note"] = "waiting to retry a failed rebalance"
        return
    try:
        sig = us_signals(fetch_daily, today)
    except Exception as e:
        US_STATUS["last_error"] = str(e)
        US_STATUS["note"] = "no rebalance: signals unavailable"
        notify.send(f"[ALERT] US ETF engine could not compute signals: {str(e)[:250]}", key="us-signal", cooldown=6 * 3600)
        return
    decision_log.log("us", "signals", mode=_us_mode(), decision=",".join(sig["selected"]),
                     reason=f"signals as of {sig['signal_date']}", month=mkey,
                     scores=sig["scores"], weights=sig["weights"])
    holdings = _us_holdings(us)
    if holdings is None:
        US_STATUS["note"] = "no rebalance: broker positions unavailable"
        notify.send("[ALERT] US ETF engine could not read broker positions; skipping this check.", key="us-pos", cooldown=6 * 3600)
        return
    if US_EXECUTE:
        open_syms = us_broker.open_order_symbols()
        if open_syms is None or (open_syms & set(us_strategy.UNIVERSE)):
            US_STATUS["note"] = "no rebalance: open orders pending or unreadable"
            notify.send("[ALERT] US ETF engine found open orders on its ETFs; waiting instead of rebalancing.", key="us-open", cooldown=6 * 3600)
            return
        if us["cash"] is None and holdings:
            mode_name = "LIVE" if US_LIVE else "paper"
            US_STATUS["note"] = f"no rebalance: {mode_name} account already holds strategy ETFs"
            notify.send(
                f"[ALERT] The {mode_name} account already holds strategy ETFs ("
                + ", ".join(sorted(holdings))
                + "). AETHER refuses to take over unknown positions.",
                key="us-preexisting",
                cooldown=12 * 3600,
            )
            return
    symbols = sorted(set(sig["weights"]) | set(holdings) | {"SPY"})
    try:
        prices = fetch_prices(symbols)
    except Exception as e:
        prices = {}
        US_STATUS["last_error"] = str(e)
    missing = [x for x in symbols if x not in prices]
    if missing:
        US_STATUS["note"] = f"no rebalance: no price for {missing}"
        notify.send(f"[ALERT] US ETF engine has no live price for {', '.join(missing)}; skipping.", key="us-price", cooldown=6 * 3600)
        return
    problems = us_strategy.price_sanity({x: prices[x] for x in symbols}, sig.get("latest_close") or {}, US_MAX_PRICE_DEV)
    if problems:
        US_STATUS["note"] = "no rebalance: price sanity check failed"
        notify.send(f"[ALERT] US rebalance skipped, price sanity check failed: {'; '.join(problems)[:300]}",
                    key="us-price-sanity", cooldown=6 * 3600)
        decision_log.log("us", "blocked", mode=_us_mode(), decision="price_sanity",
                         reason="; ".join(problems)[:300], month=mkey)
        return

    bc0, cash_start = None, None
    if US_LIVE:
        bal = us_broker.get_account_balance() or {}

        try:
            account_equity = (
                float(bal.get("portfolio_value"))
                if bal.get("source") == "live"
                else float("nan")
            )
            broker_cash = (
                float(bal.get("cash"))
                if bal.get("source") == "live"
                else float("nan")
            )
        except (TypeError, ValueError):
            account_equity = float("nan")
            broker_cash = float("nan")

        cap = US_LIVE_MAX_USD

        if not (
            0 < cap < float("inf")
            and 0 < account_equity < float("inf")
            and 0 <= broker_cash < float("inf")
        ):
            US_STATUS["note"] = "LIVE blocked: invalid account/capital data"
            notify.send(
                "[ALERT] US LIVE blocked because broker balance or "
                "US_LIVE_MAX_USD is invalid. No orders sent.",
                key="us-live-balance",
                cooldown=6 * 3600,
            )
            return

        # Only capital available to this strategy counts.
        # Unrelated account assets must not increase AETHER authority.
        strategy_holdings_value = sum(
            q * prices.get(sym, 0.0)
            for sym, q in holdings.items()
        )

        strategy_available = broker_cash + strategy_holdings_value

        equity_before = min(
            cap,
            account_equity,
            strategy_available,
        )

        if equity_before <= 0:
            US_STATUS["note"] = "LIVE blocked: no strategy capital available"
            notify.send(
                "[ALERT] US LIVE blocked because no deployable strategy "
                "capital is available. No orders sent.",
                key="us-live-no-capital",
                cooldown=6 * 3600,
            )
            return

        cash = broker_cash

    else:
        cash = us["cash"] if us["cash"] is not None else US_ALLOCATION_USD
        cash_start = cash
        if US_EXECUTE:
            bc0 = _us_broker_cash()
            if bc0 is None:
                US_STATUS["note"] = "no rebalance: paper account cash unreadable"
                notify.send("[ALERT] US paper rebalance blocked: the paper account cash could not be read. "
                            "No orders sent.", key="us-paper-cash", cooldown=6 * 3600)
                return
        equity_before = cash + sum(
            q * prices[x] for x, q in holdings.items()
        )
    orders = us_strategy.plan_orders(holdings, prices, sig["weights"], equity_before)

    # A rebalance is a ROTATION (sells, then re-buys), not an exit. The per-order gate lets
    # closing orders through a halt by design, so without this check a halt would sell the
    # portfolio and then refuse the buys -- liquidating the account while the dashboard says
    # "HALTED: all new entries blocked". Block the whole plan instead.
    if US_EXECUTE and orders and risk_controller.halted():
        US_STATUS["note"] = "no rebalance: AETHER_HALT present (rotation blocked; exits still allowed)"
        notify.send("[ALERT] US rebalance skipped: AETHER_HALT is present, so no orders were sent. "
                    "A halt blocks entries and rotations; it never blocks an exit.",
                    key="us-halted", cooldown=3600)
        decision_log.log("us", "blocked", mode=_us_mode(), decision="halted", month=mkey,
                         reason=f"AETHER_HALT present; {len(orders)} planned order(s) not sent")
        return

    # All-or-nothing: evaluate every order in the plan before submitting any of them, so the
    # sells cannot fill while the buys are refused by a per-order cap or the daily budget.
    if US_EXECUTE and orders:
        ok, why = risk_controller.preflight(
            "us",
            [(o["side"], o.get("notional", o.get("est_value")), o["side"] == "SELL", US_LIVE)
             for o in orders])
        if not ok:
            US_STATUS["note"] = f"no rebalance: risk pre-flight blocked the plan ({why})"
            notify.send(f"[ALERT] US rebalance pre-flight failed ({why}) - no orders sent. Check "
                        f"RISK_MAX_ORDER_USD_LIVE / RISK_MAX_DAILY_BUY_USD_LIVE against "
                        f"US_LIVE_MAX_USD=${US_LIVE_MAX_USD:,.2f}, and the daily buy budget.",
                        key="us-preflight", cooldown=3600)
            decision_log.log("us", "blocked", mode=_us_mode(), decision="preflight_failed",
                             reason=why, month=mkey, orders=len(orders))
            return

    if _us_gate_active() and not _us_live_gate(orders, sig, mkey, today, now_et, equity_before):
        return
    if US_EXECUTE:
        attempt = sum(1 for h in (us.get("history") or []) if h.get("month") == mkey)   # earlier attempts this month
        results, holdings, cash = _us_execute_paper(orders, prices, cash, holdings, mkey=mkey, attempt=attempt)
    else:
        results, holdings, cash = _us_execute_shadow(orders, prices, holdings, cash)
    failed = [r for r in results if r["state"] not in ("filled", "simulated")]
    equity_after = cash + sum(q * prices.get(x, 0.0) for x, q in holdings.items())

    with STATE_LOCK:
        state = load_state()
        u = _us_state(state)
        u.update(cash=cash, holdings=holdings, targets=sig["weights"], scores=sig["scores"],
                 selected=sig["selected"], signal_date=sig["signal_date"], last_prices=prices)
        if bc0 is not None:   # the account cash that matches the sleeve cash just stored
            u["broker_cash_at_save"] = bc0 + (cash - cash_start)
        if not failed:
            u["last_rebalance_month"] = mkey
            u["last_rebalance_at"] = utcnow_str()
        stuck = [r.get("order_id") for r in results if r["state"] == "unreconciled"]
        if stuck:
            u["frozen"] = f"unreconciled order(s) {stuck}: check the broker, then clear us.frozen"
        elif US_EXECUTE and us_broker.consecutive_errors >= BROKER_ERROR_FREEZE_THRESHOLD:
            # The broker call itself failed BROKER_ERROR_FREEZE_THRESHOLD times in a row (not an
            # order rejection -- a timeout, a 5xx, a dropped connection). Retrying forever against
            # a broker that is genuinely down just keeps failing silently; freeze instead, the same
            # way an unreconciled order already does, so this surfaces instead of looping quietly.
            u["frozen"] = (f"{us_broker.consecutive_errors} consecutive broker errors: "
                           f"check Alpaca connectivity, then clear us.frozen")
        u["history"].append({
            "date": today, "month": mkey, "signal_date": sig["signal_date"], "mode": _us_mode(),
            "weights": sig["weights"], "equity": round(equity_after, 2), "complete": not failed,
            "orders": [{"symbol": r["symbol"], "side": r["side"], "state": r["state"],
                        "qty": round(r.get("filled_qty", 0.0) or 0.0, 6),
                        "price": round(r.get("avg_price") or 0.0, 2)} for r in results]})
        u["history"] = u["history"][-24:]
        save_state(state)

    decision_log.log("us", "rebalance", mode=_us_mode(), decision="complete" if not failed else "incomplete",
                     reason=f"{len(results)} order(s), {len(failed)} failed", month=mkey,
                     equity_before=round(equity_before, 2), equity_after=round(equity_after, 2),
                     orders=[{k: r.get(k) for k in ("symbol", "side", "state", "qty", "notional", "filled_qty",
                                                    "avg_price", "detail", "order_id")}
                             for r in results])
    US_STATUS["note"] = "rebalanced" if not failed else "rebalance incomplete; will retry"
    US_STATUS["last_error"] = None if not failed else f"{len(failed)} order(s) failed"
    if failed:
        US_STATUS["retry_after"] = time.time() + US_RETRY_SECONDS
    held = ", ".join(f"{x} {w * 100:.0f}%" for x, w in sorted(sig["weights"].items()))
    lines = [f"[US] Monthly rebalance ({_us_mode()}){' INCOMPLETE, will retry' if failed else ''} {uae_str()}", f"Signals as of {sig['signal_date']}: holding {held}"]
    for r in results:
        amt = f"${r['notional']:,.2f}" if r["side"] == "BUY" else f"{r['qty']:.4f} sh"
        lines.append(f"  {r['side']} {r['symbol']} {amt} -> {r['state']}"
                     + (f" ({str(r['detail'])[:120]})" if r.get("detail") else ""))
    if not results:
        lines.append("  No trades needed.")
    lines.append(f"Equity ${equity_after:,.2f} (cash ${cash:,.2f})")
    notify.send("\n".join(lines))
    if failed:
        # The broker's own reason goes into the alert, and the de-dup key includes which orders
        # failed and how, so a DIFFERENT failure inside the hour is not silenced by the first.
        what = ", ".join(f"{r['symbol']} {r['side']} {r['state']}" + (f": {str(r['detail'])[:120]}" if r.get("detail") else "")
                         for r in failed)
        sig_key = "|".join(sorted(f"{r['symbol']}:{r['side']}:{r['state']}" for r in failed))
        notify.send(f"[ALERT] {len(failed)} US ETF order(s) did not fill: {what}. Will retry.",
                    key=f"us-fail:{sig_key}", cooldown=3600)


def _us_gate_active() -> bool:
    return US_EXECUTE and (US_LIVE or US_REQUIRE_APPROVAL)


def _us_live_gate(orders, sig, mkey, today, now_et, equity_before) -> bool:
    """Approval gate: always on in LIVE mode, optional in PAPER (US_REQUIRE_APPROVAL=true).
    Returns True only when this exact plan was approved on Telegram today and it is past 15:30 ET.
    Otherwise it creates or keeps a proposal and returns False, so nothing is sent."""
    mode = _us_mode()
    if US_LIVE:
        if us_broker.client is None:
            US_STATUS["note"] = "LIVE blocked: live Alpaca connection unavailable"
            notify.send("[ALERT] US LIVE mode is on but the live Alpaca connection is unavailable "
                        "(check APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY). No orders sent.",
                        key="us-live-client", cooldown=6 * 3600)
            decision_log.log_changed("us:gate", ("client", today), engine="us", event="live_blocked", mode=mode,
                                     reason="live client unavailable", month=mkey)
            return False
        buys = sum(o.get("notional", 0.0) for o in orders if o["side"] == "BUY")
        bal = us_broker.get_account_balance() or {}
        try:
            acct_eq = float(bal.get("portfolio_value")) if bal.get("source") == "live" else float("nan")
        except (TypeError, ValueError):
            acct_eq = float("nan")
        cap = US_LIVE_MAX_USD
        if not (
            0 < cap < float("inf")
            and 0 <= equity_before <= cap
            and 0 <= buys <= cap
            and 0 < acct_eq < float("inf")
        ):
            why = (
                f"strategy authority ${equity_before:,.2f}, "
                f"buys ${buys:,.2f}, "
                f"account equity ${acct_eq:,.2f}, "
                f"US_LIVE_MAX_USD=${cap:,.2f}"
            )
            US_STATUS["note"] = "LIVE blocked by dollar cap"
            notify.send(f"[ALERT] US LIVE rebalance blocked by the dollar cap ({why}). No orders sent.",
                        key=f"us-live-cap:{today}", cooldown=6 * 3600)
            decision_log.log_changed("us:gate", ("cap", today), engine="us", event="live_blocked", mode=mode,
                                     reason=why, month=mkey)
            return False
    if not orders:
        return True
    account = us_broker.account_id()
    if US_LIVE and not account:
        US_STATUS["note"] = "LIVE blocked: live account id unreadable"
        decision_log.log_changed("us:gate", ("account", today), engine="us", event="live_blocked", mode=mode,
                                 reason="live account id unreadable", month=mkey)
        return False
    key = json.dumps({"month": mkey, "mode": mode, "account": account or "unknown",
                      "weights": {k: round(v, 4) for k, v in sorted(sig["weights"].items())},
                      "orders": sorted([o["symbol"], o["side"]] for o in orders)}, sort_keys=True)
    after_cutoff = (now_et.hour, now_et.minute) >= US_EXEC_AFTER_ET
    with STATE_LOCK:
        state = load_state()
        p = _us_state(state).get("live_proposal")
        if p and p.get("key") == key and p.get("date") == today:
            if p["status"] == "PENDING":
                US_STATUS["note"] = f"awaiting Telegram approval of proposal {p['id']}"
                return False
            if p["status"] == "REJECTED":
                US_STATUS["note"] = f"proposal {p['id']} rejected; next attempt on the next trading day"
                return False
            if p["status"] == "APPROVED" and _us_within_approval(orders, p.get("orders") or []):
                if not after_cutoff:
                    US_STATUS["note"] = f"proposal {p['id']} approved; orders go out after 15:30 ET"
                    return False
                p["status"] = "USED"
                p["used_at"] = utcnow_str()
                save_state(state)
                decision_log.log("us", "approval_used", mode=mode, decision="execute",
                                 reason=f"proposal {p['id']}", month=mkey)
                return True
        if p and p.get("status") in ("PENDING", "APPROVED"):
            p["status"] = "EXPIRED"
            decision_log.log("us", "proposal_expired", mode=mode,
                             reason=f"proposal {p['id']} replaced (plan or day changed)", month=mkey)
        pid = os.urandom(3).hex()
        _us_state(state)["live_proposal"] = {"id": pid, "key": key, "date": today, "month": mkey, "status": "PENDING",
                                        "created_at": utcnow_str(), "orders": orders, "weights": sig["weights"]}
        save_state(state)
    lines = [f"[US {mode}] Rebalance proposal {pid} for {mkey}",
             "Targets: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(sig["weights"].items())),
             f"Equity about ${equity_before:,.2f}"]
    for o in orders:
        amt = f"${o['notional']:,.2f}" if o["side"] == "BUY" else f"{o['qty']:.4f} sh"
        lines.append(f"  {o['side']} {o['symbol']} {amt}")
    lines.append(f"Approve: /approve_{pid}    Reject: /reject_{pid}")
    if now_et.day > 4:
        lines.append("Note: catch-up rebalance (later than the month's first trading days).")
    lines.append("Valid today only. If approved, orders go out after 15:30 ET.")
    notify.send("\n".join(lines))
    decision_log.log("us", "proposal", mode=mode, decision="PENDING", reason=f"proposal {pid}", month=mkey,
                     orders=orders, weights=sig["weights"], catch_up=now_et.day > 4)
    US_STATUS["note"] = f"awaiting Telegram approval of proposal {pid}"
    return False


US_APPROVAL_TOLERANCE = 0.02


def _us_within_approval(orders, approved) -> bool:
    """True if every order matches an approved order (same symbol and side) and is no larger than approved
    (+2% for price moves), and total buying is within the approved total. Smaller orders are fine."""
    ok = {(a["symbol"], a["side"]): a for a in approved}
    tol = 1 + US_APPROVAL_TOLERANCE
    for o in orders:
        a = ok.get((o["symbol"], o["side"]))
        if a is None:
            return False
        if o["side"] == "BUY" and o.get("notional", 0.0) > a.get("notional", 0.0) * tol + 0.01:
            return False
        if o["side"] == "SELL" and o.get("qty", 0.0) > a.get("qty", 0.0) * tol + 1e-6:
            return False
    buys = sum(o.get("notional", 0.0) for o in orders if o["side"] == "BUY")
    return buys <= sum(a.get("notional", 0.0) for a in approved if a["side"] == "BUY") * tol + 0.01


def _us_live_decide(cmd: str) -> str:
    """Telegram /approve_<id> or /reject_<id>. Only the configured chat can reach this (see notify.py)."""
    action, _, pid = cmd.lstrip("/").partition("_")
    today = datetime.now(timezone.utc).astimezone(NY).strftime("%Y-%m-%d")
    with STATE_LOCK:
        state = load_state()
        p = _us_state(state).get("live_proposal")
        if not p or p.get("id") != pid:
            return "No matching US proposal. It may have expired or been replaced; check the latest message."
        if p["status"] != "PENDING":
            return f"Proposal {pid} is already {p['status']}; nothing changed."
        if p["date"] != today:
            p["status"] = "EXPIRED"
            save_state(state)
            return f"Proposal {pid} was for {p['date']} and has expired. A new one will be sent."
        p["status"] = "APPROVED" if action == "approve" else "REJECTED"
        p["decided_at"] = utcnow_str()
        save_state(state)
        status, month = p["status"], p["month"]
    decision_log.log("us", "approval", mode=_us_mode(), decision=status, reason=f"proposal {pid}", month=month)
    if status == "APPROVED":
        return f"US proposal {pid} APPROVED. Orders go out after 15:30 ET if the plan is unchanged."
    return f"US proposal {pid} REJECTED. No US orders today."


def _us_snapshot(fetch_prices, today: str):
    with STATE_LOCK:
        us = _us_state(load_state())
    holdings = _us_holdings(us)
    if holdings is None or us["cash"] is None:
        return
    prices = fetch_prices(sorted(set(holdings) | {"SPY"}))
    if any(x not in prices for x in set(holdings) | {"SPY"}):
        return
    if _us_reconcile_cash(us):
        return
    equity = us["cash"] + sum(q * prices[x] for x, q in holdings.items())
    with STATE_LOCK:
        state = load_state()
        u = _us_state(state)
        u["equity_history"].append({"d": today, "v": round(equity, 2), "spy": prices["SPY"]})
        u["equity_history"] = u["equity_history"][-800:]
        u["last_snapshot_date"] = today
        u["last_prices"].update(prices)
        save_state(state)


def us_cycle(fetch_daily=None, fetch_prices=None, market=None):
    try:
        return _us_cycle_inner(fetch_daily, fetch_prices, market)
    finally:
        decision_log.log_changed("us:status", US_STATUS.get("note"), engine="us", event="status",
                                 mode=_us_mode(), reason=US_STATUS.get("note"),
                                 last_error=US_STATUS.get("last_error"))


def _us_set_kill(killed: bool, reason: str) -> bool:
    """Record the US kill-switch state, alert on a new trip, and log every change."""
    was = US_STATUS.get("kill_switch")
    US_STATUS["kill_switch"] = killed
    US_STATUS["kill_reason"] = None if not killed else reason
    risk_controller.set_kill("us", killed)
    if killed and was is not True:
        notify.send(f"[ALERT] US kill switch ACTIVE: {reason}. New US entries and rotations are "
                    f"blocked until it clears. Nothing sells automatically: review the positions "
                    f"and close by hand if needed.", key="us-kill", cooldown=6 * 3600)
    if was is not None and killed != was:
        decision_log.log("us", "kill_switch", decision="active" if killed else "cleared",
                         mode=_us_mode(), reason=reason)
    return killed


def _us_evaluate_kill_switch(us: dict, today: str) -> bool:
    """Daily-loss / peak-drawdown kill switch for the US engine.

    Runs once per US cycle, before any rebalance decision, so it keeps evaluating while the
    market is closed. Fails CLOSED on unknown data: when positions cannot be valued the
    previous state is left untouched rather than cleared (the start-up state is None, which
    blocks entries until an engine reports).
    """
    if us.get("dd_latched"):
        # A peak-drawdown trip is latched (strategy.md: re-enable only after a written review).
        # Clearing is a manual step: stop the service, review, set us.dd_latched to null AND
        # re-baseline us.equity_history, or the same peak trips it again on the next cycle.
        return _us_set_kill(True, f"drawdown kill latched: {us['dd_latched']} (review required)")
    holdings = {s: q for s, q in (us.get("holdings") or {}).items() if q > 0}
    prices = us.get("last_prices") or {}
    cash = us.get("cash")

    if not holdings:
        # Nothing is deployed, so there is no loss to guard against. This must be reported as
        # CLEAR rather than left unevaluated: cash only becomes non-None after a rebalance, so
        # leaving it unevaluated would block the first rebalance forever.
        return _us_set_kill(False, "no open US positions")

    missing = [s for s in holdings if s not in prices]
    if cash is None or missing:
        logger.warning(f"US kill switch not evaluated: cash={cash}, missing prices={missing}")
        return bool(US_STATUS.get("kill_switch"))  # never clear on unknown data

    equity = float(cash) + sum(q * prices[s] for s, q in holdings.items())

    hist = [h for h in (us.get("equity_history") or []) if h.get("v")]
    if not hist:
        logger.warning("US kill switch not evaluated: no equity-history baseline yet")
        return bool(US_STATUS.get("kill_switch"))

    day_start = None
    for h in reversed(hist):
        if h.get("d") == today:
            day_start = float(h["v"])
            break
    if day_start is None:
        # Today's snapshot is only written after the close, so use the last one as the basis.
        prior = [h for h in hist if h.get("d") != today]
        if prior:
            day_start = float(prior[-1]["v"])
    peak = max([float(h["v"]) for h in hist] + [equity])

    daily_loss = (day_start - equity) / day_start if day_start and day_start > 0 else 0.0
    drawdown = (peak - equity) / peak if peak > 0 else 0.0

    if daily_loss >= US_MAX_DAILY_LOSS:
        return _us_set_kill(True, f"down {daily_loss * 100:.2f}% today "
                                  f"(limit {US_MAX_DAILY_LOSS * 100:g}%)")
    if drawdown >= US_MAX_DRAWDOWN:
        reason = (f"{drawdown * 100:.2f}% below peak equity "
                  f"(limit {US_MAX_DRAWDOWN * 100:g}%)")
        us["dd_latched"] = reason
        try:
            with STATE_LOCK:
                st = load_state()
                _us_state(st)["dd_latched"] = reason
                save_state(st)
        except Exception as e:  # still block entries this cycle; the next cycle retries the save
            logger.error(f"Could not persist the US drawdown latch: {type(e).__name__}: {str(e)[:120]}")
        return _us_set_kill(True, reason)
    return _us_set_kill(False, f"ok (today {daily_loss * 100:+.2f}%, from peak -{drawdown * 100:.2f}%)")


def _us_cycle_inner(fetch_daily=None, fetch_prices=None, market=None):
    fetch_daily = fetch_daily or fetch_us_daily
    fetch_prices = fetch_prices or fetch_us_prices
    with US_LOCK:
        is_open, now_et = (market or us_market_state)()
        today, mkey = now_et.strftime("%Y-%m-%d"), now_et.strftime("%Y-%m")
        US_STATUS["last_check"] = utcnow_str()
        US_STATUS["last_check_ts"] = time.time()
        with STATE_LOCK:
            us = _us_state(load_state())
        cash_problem = _us_reconcile_cash(us)
        if cash_problem:
            US_STATUS["note"] = f"paper cash needs review: {cash_problem}"
            notify.send(f"[ALERT] US paper engine paused: {cash_problem}. No orders will be sent until it is "
                        f"resolved (stop the service, check the paper account, then clear "
                        f"us_paper.broker_cash_at_save in the state file).", key="us-paper-cash", cooldown=6 * 3600)
            return
        _us_evaluate_kill_switch(us, today)
        if (not is_open) and now_et.weekday() < 5 and now_et.hour >= 16 and us["cash"] is not None \
                and us["last_snapshot_date"] != today:
            try:
                _us_snapshot(fetch_prices, today)
            except Exception as e:
                logger.warning(f"US snapshot failed: {type(e).__name__}: {str(e)[:120]}")
        if time.time() - US_STATUS["preview_at"] > 3600:
            US_STATUS["preview_at"] = time.time()
            try:
                US_STATUS["preview"] = {**us_signals(fetch_daily, today), "as_of": utcnow_str()}
            except Exception as e:
                US_STATUS["note"] = f"signal preview unavailable: {str(e)[:120]}"
        if not is_open:
            US_STATUS["note"] = "US market closed"
            return
        if us["last_rebalance_month"] == mkey:
            US_STATUS["note"] = f"already rebalanced for {mkey}"
            return
        if (now_et.hour, now_et.minute) < US_EXEC_AFTER_ET and not _us_gate_active():
            US_STATUS["note"] = "waiting until 15:30 ET to rebalance"
            return
        _us_rebalance(us, today, mkey, now_et, fetch_daily, fetch_prices)


def us_loop():
    logger.info(f"US ETF momentum engine started ({_us_mode()} mode, allocation ${(US_LIVE_MAX_USD if US_LIVE else US_ALLOCATION_USD):,.0f}, "
                f"checks every {US_CHECK_INTERVAL}s).")
    while not US_STOP.is_set():
        try:
            us_cycle()
        except Exception as e:
            logger.exception("US cycle failed")
            US_STATUS["last_error"] = str(e)[:200]
            notify.send(f"[ALERT] US ETF engine error: {str(e)[:250]}", key="us-error", cooldown=3600)
        US_STOP.wait(US_CHECK_INTERVAL)


@app.get("/api/us")
async def get_us_status():
    """US ETF momentum engine: signals, targets, holdings, equity curve and rebalance history."""
    pass  # auth: _dashboard_auth session middleware
    with STATE_LOCK:
        us = _us_state(load_state())
    prev = US_STATUS.get("preview") or {}
    scores = us["scores"] or prev.get("scores") or {}
    ranked = sorted(scores, key=lambda x: (-scores[x], x))
    prices = us["last_prices"]
    rows = []
    for sym in us_strategy.UNIVERSE:
        qty = us["holdings"].get(sym, 0.0)
        rows.append({"symbol": sym, "score": scores.get(sym), "rank": (ranked.index(sym) + 1) if sym in ranked else None,
                     "target": us["targets"].get(sym, 0.0), "qty": qty, "value": qty * prices.get(sym, 0.0)})
    equity = (us["cash"] + sum(r["value"] for r in rows)) if us["cash"] is not None else None
    return {
        "status": "success", "enabled": US_ENABLED, "mode": _us_mode(), "allocation_usd": (US_LIVE_MAX_USD if US_LIVE else US_ALLOCATION_USD),
        "last_check": US_STATUS["last_check"], "last_error": US_STATUS["last_error"], "note": US_STATUS["note"],
        "last_rebalance_month": us["last_rebalance_month"], "last_rebalance_at": us["last_rebalance_at"],
        "signal_date": us["signal_date"], "preview": prev if not us["scores"] else None,
        "cash": us["cash"], "equity": equity, "rows": rows,
        "kill_switch": US_STATUS.get("kill_switch"), "kill_reason": US_STATUS.get("kill_reason"),
        "equity_history": us["equity_history"], "history": list(reversed(us["history"]))[:12],
    }


# The legacy inline dashboard (/classic, /account, /api/stocks, /api/history,
# /api/portfolio-history, /api/snapshot) was removed 2026-10-03: superseded by
# dashboard_v2.html + dashboard_classic.html, unlinked from any current UI, and
# it had a confirmed stored-XSS hole (unescaped broker/webhook text via innerHTML)
# that patching wasn't worth doing in dead code nobody could reach through the app.
import dashboard_v2 as _dashboard_v2  # noqa: E402
app.include_router(_dashboard_v2.router)

# aether_us_dashboard package (2026-10-04): a dedicated US-only view at /us, reading safety_guard's
# live-account breakers and the decision log. AETHER's auth is the _dashboard_auth middleware
# above, not a FastAPI Depends() dependency, so build_router gets a no-op and relies on that
# middleware (which already covers every path, including this one, by default-deny).
from us_dashboard import build_router as _build_us_dashboard_router  # noqa: E402
app.include_router(_build_us_dashboard_router(lambda: None))

# us_extras (2026-10-13): GET /api/us/extras -- orders lifecycle, server health,
# research/DSR gate, backups. Same no-op auth_dep as us_dashboard above; the real
# gate is the _dashboard_auth middleware, default-deny.
from us_extras import build_router as _build_us_extras_router  # noqa: E402
app.include_router(_build_us_extras_router(lambda: None))

# map_status (2026-10-18): GET /map (animated live system map) and GET /api/map/status.
# Read-only; same default-deny _dashboard_auth middleware as every other page.
from map_status import build_router as _build_map_router  # noqa: E402


def _map_engine_info() -> dict:
    return {"us": {k: US_STATUS.get(k) for k in ("last_check", "last_error", "note", "kill_switch", "kill_reason")},
            "crypto": {k: STRATEGY_STATUS.get(k) for k in ("last_cycle", "last_error", "kill_switch")}}


app.include_router(_build_map_router(lambda: None, _map_engine_info))

# connectivity (2026-10-18): GET /api/connectivity, read-only link checks (Cloudflare, tunnel, Telegram,
# GitHub, Alpaca, internet). Same default-deny _dashboard_auth middleware as every other page.
from connectivity import build_router as _build_connectivity_router  # noqa: E402
app.include_router(_build_connectivity_router(lambda: None))

# trend_gate (2026-10-21): GET /signal/{symbol}, the SPY 200-day trend gate (advisory, read-only,
# paper data keys by default). Same default-deny _dashboard_auth middleware as every other page.
from trend_gate.api import build_router as _build_trend_gate_router  # noqa: E402
app.include_router(_build_trend_gate_router())
# aether_tactical (2026-10-21): GET /tactical/signal/{symbol} and /tactical/ledger, the pre-registered
# ORB and engulfing PAPER tests (strategy.md "Tactical paper tests"). Read-only; same login middleware.
from aether_tactical.api_tactical import build_router as _build_tactical_router  # noqa: E402
app.include_router(_build_tactical_router())

# autotest_view (2026-10-21): GET /autotest page + /api/autotest. Reads the automatic live test's
# status FILE and decision-log rows only; no broker calls and no import of the test-trading code.
from autotest_view import build_router as _build_autotest_router  # noqa: E402
app.include_router(_build_autotest_router())

# research_view (2026-10-25): GET /research page + /api/research. Strategy registry, evidence by stage
# (historical / forward / paper / live), forward-journal health, trial count. Files only; no broker calls.
from research_view import build_router as _build_research_router  # noqa: E402
app.include_router(_build_research_router())
