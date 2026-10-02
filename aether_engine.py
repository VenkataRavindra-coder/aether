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
from alpaca_executor import AlpacaExecutionClient
import strategy as strat
import us_strategy
import notify
import decision_log
import risk_controller
from zoneinfo import ZoneInfo

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AETHER_CORE")

@asynccontextmanager
async def lifespan(_app):
    # Decide WHY we started before any background thread exists: the monitor thread rewrites the
    # heartbeat file the moment it runs, which would erase the evidence this check reads.
    report = startup_report()
    if STRATEGY_ENABLED:
        threading.Thread(target=strategy_loop, daemon=True, name="aether-strategy").start()
    else:
        logger.info("Strategy engine disabled (set STRATEGY_ENABLED=true in .env to enable).")
    if US_ENABLED:
        threading.Thread(target=us_loop, daemon=True, name="aether-us").start()
    threading.Thread(target=monitor_loop, daemon=True, name="aether-monitor").start()
    notify.start_command_poller(handle_command)
    logger.info(f"Telegram alerts {'enabled' if notify.enabled() else 'disabled (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set)'}.")
    notify.send(report, key="startup", cooldown=60)
    write_heartbeat()
    yield
    _shutting_down.set()
    notify.send("[AETHER] Service stopping (VM shutdown, restart or manual stop).", key="shutdown", cooldown=60)
    write_heartbeat(clean_stop=True)
    time.sleep(2)
    STRATEGY_STOP.set()
    US_STOP.set()
    notify.STOP.set()


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
US_LIVE_MAX_USD = float(os.getenv("US_LIVE_MAX_USD", "0"))
US_REQUIRE_APPROVAL = US_LIVE or os.getenv("US_REQUIRE_APPROVAL", "false").lower() == "true"
us_broker = (AlpacaExecutionClient(paper=False, key_env="APCA_LIVE_API_KEY_ID",
                                   secret_env="APCA_LIVE_API_SECRET_KEY") if US_LIVE else broker)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aether_state.json")
STATE_LOCK = threading.Lock()
STARTED_AT = datetime.now(timezone.utc)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Webhook BUY size is a dollar amount (converted to quantity at the alert price), never a raw
# unit count, and any order above MAX_ORDER_PCT of account equity is rejected outright.
TRADE_NOTIONAL_USD = float(os.getenv("TRADE_NOTIONAL_USD", "100"))
MAX_ORDER_PCT = float(os.getenv("MAX_ORDER_PCT", "5")) / 100

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


def load_state() -> dict:
    """Load persistent state with automatic legacy-US migration."""
    state = default_state()

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

            return state

        except Exception as e:
            logger.error(f"Failed to read state file {path}: {str(e)}")

    return state


def save_state(state: dict):
    """Atomic write: a crash or power loss mid-write can never leave a truncated state file.
    The previous good copy is kept as .bak, and load_state falls back to it if needed."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(STATE_FILE):
        shutil.copyfile(STATE_FILE, STATE_FILE + ".bak")
    os.replace(tmp, STATE_FILE)


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
STRATEGY_INTERVAL = int(os.getenv("STRATEGY_INTERVAL_SECONDS", "60"))
STRATEGY_COOLDOWN = int(os.getenv("STRATEGY_COOLDOWN_SECONDS", "7200"))
STRATEGY_MAX_DAILY_LOSS = float(os.getenv("STRATEGY_MAX_DAILY_LOSS_PCT", "3")) / 100
STRATEGY_SHADOW_EQUITY = float(os.getenv("STRATEGY_SHADOW_EQUITY", "100000"))
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


@app.middleware("http")
async def _dashboard_session(request: _Request, call_next):
    path = request.url.path
    if path not in _OPEN_PATHS:
        sid = request.cookies.get(_SESSION_COOKIE, "")
        if sid and _session_ok(sid):
            raw = request.scope.get("query_string", b"").decode()
            pairs = [(k, v) for k, v in _parse_qsl(raw, keep_blank_values=True) if k != "secret"]
            pairs.append(("secret", WEBHOOK_SECRET))
            request.scope["query_string"] = _urlencode(pairs).encode()
        elif path == "/" and "secret" not in request.query_params:
            return _RedirectResponse("/login", status_code=303)
    return await call_next(request)


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


@app.post("/login")
def _login(password: str = _Form(default="")):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="DASHBOARD_PASSWORD is not set in .env")
    if not secrets.compare_digest(password.encode(), DASHBOARD_PASSWORD.encode()):
        _time.sleep(1)
        raise HTTPException(status_code=401, detail="Wrong password")
    sid = _session_token()
    resp = _RedirectResponse("/", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, sid, httponly=True, secure=True, samesite="lax", max_age=_SESSION_DAYS * 86400)
    return resp


@app.get("/logout")
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

_PUBLIC_PATHS = {"/login", "/logout", "/webhook/tradingview"}


@app.middleware("http")
async def _dashboard_auth(request: _Request, call_next):
    """Every path except login/logout and the TradingView webhook needs a dashboard session.
    Fails closed: unknown paths (including /docs and /openapi.json) are refused too."""
    path = request.url.path
    sid = request.cookies.get(_SESSION_COOKIE, "")
    if path in _PUBLIC_PATHS or (sid and _session_ok(sid)):
        return await call_next(request)
    if request.method == "GET" and path == "/":
        return _RedirectResponse("/login", status_code=303)
    return _JSONResponse({"detail": "Not logged in."}, status_code=401)


@app.get("/api/strategy")
async def get_strategy_status(secret: str = Query(default="")):
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
def run_backtest(symbol: str = Query(default="BTC/USD"), days: int = Query(default=365),
                 secret: str = Query(default="")):
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


DIGEST_HOUR_UTC = int(os.getenv("DIGEST_HOUR_UTC", "6"))
HEALTHCHECK_PING_URL = os.getenv("HEALTHCHECK_PING_URL", "")
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
        urllib.request.urlopen(HEALTHCHECK_PING_URL, timeout=10).read()
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


def service_states() -> dict:
    """systemd state of each entry in MONITOR_SERVICES ({} when systemd is unavailable).
    An entry may list alternatives with '|' (e.g. 'ssh|ssh.socket'); it counts as active if
    any alternative is active, which matters for socket-activated services."""
    out = {}
    for spec in MONITOR_SERVICES:
        states = []
        for unit in spec.split("|"):
            try:
                r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
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


def monitor_cycle(now: datetime = None):
    now = now or datetime.now(timezone.utc)
    write_heartbeat()
    check_services()
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

    if HEALTHCHECK_PING_URL and not stalled:
        _ping_healthcheck()

    today = now.strftime("%Y-%m-%d")
    if now.hour == DIGEST_HOUR_UTC and _monitor["digest_date"] != today:
        _monitor["digest_date"] = today
        notify.send(build_status_text("AETHER daily digest"))


def monitor_loop():
    while not notify.STOP.is_set() and not _shutting_down.is_set():
        try:
            monitor_cycle()
        except Exception:
            logger.exception("Monitor cycle failed")
        notify.STOP.wait(MONITOR_INTERVAL)


US_ENABLED = os.getenv("US_ENABLED", "false").lower() == "true"
US_EXECUTE = os.getenv("US_EXECUTE", "false").lower() == "true"       # false = simulated (shadow); true = orders on the Alpaca PAPER account
US_ALLOCATION_USD = float(os.getenv("US_ALLOCATION_USD", "5000"))
US_CHECK_INTERVAL = int(os.getenv("US_CHECK_INTERVAL_SECONDS", "300"))
US_EXEC_AFTER_ET = (15, 30)
US_SLIPPAGE = 0.0005
US_RETRY_SECONDS = 900
NY = ZoneInfo("America/New_York")
UAE_TZ = timezone(timedelta(hours=4))
US_STOP = threading.Event()
US_LOCK = threading.Lock()
US_STATUS = {"last_check": None, "last_error": None, "note": "", "preview": None, "preview_at": 0.0, "retry_after": 0.0}
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
        closes[sym] = [b["c"] for b in bars]
        if bars:
            last_dates.append(bars[-1]["d"])
    weights, scores, selected = us_strategy.target_weights(closes)
    signal_date = min(last_dates)
    prev_month = (datetime.strptime(today_str[:7] + "-01", "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m")
    if signal_date[:7] != prev_month:
        raise ValueError(f"stale data: newest completed-month bar is {signal_date}, expected a date in {prev_month}")
    return {"weights": weights, "scores": scores, "selected": selected, "signal_date": signal_date}


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


def _us_execute_paper(orders, prices, cash, holdings_before=None):
    """Sends orders and books only broker-confirmed fills. An order still open at the poll timeout is
    cancelled and re-read, so a late fill cannot leave cash stale. An order that still cannot be
    reconciled is marked 'unreconciled' (the engine then freezes). If positions cannot be read after
    the fills, the confirmed fills are booked onto the known holdings and the rebalance stays incomplete."""
    results = []
    book = dict(holdings_before or {})
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "SELL":
            r = us_broker.submit_market_order(sym, "SELL", qty=o["qty"])
        else:
            r = us_broker.submit_market_order(sym, "BUY", notional=o["notional"])
        if r.get("status") != "submitted":
            results.append({**o, "state": "error", "detail": r.get("message")})
            continue
        f = us_broker.wait_for_fill(r["order_id"])
        if f["state"] not in _US_FINAL_STATES:
            us_broker.cancel_order(r["order_id"])
            f = us_broker.wait_for_fill(r["order_id"], timeout=60.0)
        qty = float(f.get("filled_qty") or 0.0)
        px = f["avg_price"] or prices[sym]
        cash += qty * px if o["side"] == "SELL" else -qty * px
        book[sym] = book.get(sym, 0.0) + (qty if o["side"] == "BUY" else -qty)
        state = f["state"] if f["state"] in _US_FINAL_STATES else "unreconciled"
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
        equity_before = cash + sum(
            q * prices[x] for x, q in holdings.items()
        )
    orders = us_strategy.plan_orders(holdings, prices, sig["weights"], equity_before)
    if _us_gate_active() and not _us_live_gate(orders, sig, mkey, today, now_et, equity_before):
        return
    if US_EXECUTE:
        results, holdings, cash = _us_execute_paper(orders, prices, cash, holdings)
    else:
        results, holdings, cash = _us_execute_shadow(orders, prices, holdings, cash)
    failed = [r for r in results if r["state"] not in ("filled", "simulated")]
    equity_after = cash + sum(q * prices.get(x, 0.0) for x, q in holdings.items())

    with STATE_LOCK:
        state = load_state()
        u = _us_state(state)
        u.update(cash=cash, holdings=holdings, targets=sig["weights"], scores=sig["scores"],
                 selected=sig["selected"], signal_date=sig["signal_date"], last_prices=prices)
        if not failed:
            u["last_rebalance_month"] = mkey
            u["last_rebalance_at"] = utcnow_str()
        stuck = [r.get("order_id") for r in results if r["state"] == "unreconciled"]
        if stuck:
            u["frozen"] = f"unreconciled order(s) {stuck}: check the broker, then clear us.frozen"
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
                     orders=[{k: r.get(k) for k in ("symbol", "side", "state", "qty", "notional", "filled_qty", "avg_price")}
                             for r in results])
    US_STATUS["note"] = "rebalanced" if not failed else "rebalance incomplete; will retry"
    US_STATUS["last_error"] = None if not failed else f"{len(failed)} order(s) failed"
    if failed:
        US_STATUS["retry_after"] = time.time() + US_RETRY_SECONDS
    held = ", ".join(f"{x} {w * 100:.0f}%" for x, w in sorted(sig["weights"].items()))
    lines = [f"[US] Monthly rebalance ({_us_mode()}) {uae_str()}", f"Signals as of {sig['signal_date']}: holding {held}"]
    for r in results:
        amt = f"${r['notional']:,.2f}" if r["side"] == "BUY" else f"{r['qty']:.4f} sh"
        lines.append(f"  {r['side']} {r['symbol']} {amt} -> {r['state']}")
    if not results:
        lines.append("  No trades needed.")
    lines.append(f"Equity ${equity_after:,.2f} (cash ${cash:,.2f})")
    notify.send("\n".join(lines))
    if failed:
        notify.send(f"[ALERT] {len(failed)} US ETF order(s) did not fill: " +
                    ", ".join(f"{r['symbol']} {r['state']}" for r in failed) + ". Will retry.", key="us-fail", cooldown=3600)


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


def _us_cycle_inner(fetch_daily=None, fetch_prices=None, market=None):
    fetch_daily = fetch_daily or fetch_us_daily
    fetch_prices = fetch_prices or fetch_us_prices
    with US_LOCK:
        is_open, now_et = (market or us_market_state)()
        today, mkey = now_et.strftime("%Y-%m-%d"), now_et.strftime("%Y-%m")
        US_STATUS["last_check"] = utcnow_str()
        with STATE_LOCK:
            us = _us_state(load_state())
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
async def get_us_status(secret: str = Query(default="")):
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
        "equity_history": us["equity_history"], "history": list(reversed(us["history"]))[:12],
    }


@app.get("/classic", response_class=HTMLResponse)
async def visual_pipeline_dashboard(secret: str = Query(default="")):
    """Renders the secure visual AETHER operations dashboard UI."""
    verify_access(secret=secret)

    is_live = os.getenv("LIVE_TRADING", "false").lower() == "true"
    mode_label = "LIVE EXECUTION" if is_live else "OBSERVATION (SHADOW)"
    mode_color = "#238636" if is_live else "#1f6feb"

    # NOTE: this is a PLAIN triple-quoted string, not an f-string. Embedding this much
    # JS (with its own `{`/`}` blocks and `${...}` template literals) inside an f-string
    # would have every unescaped brace parsed as a Python replacement field. Using a
    # plain string and substituting the handful of dynamic values with .replace() avoids
    # that entirely instead of hand-escaping every brace in the JS.
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AETHER Autonomous Trading Pipeline</title>
        <style>
            body { background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 25px; }
            header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #30363d; padding-bottom: 20px; margin-bottom: 25px; flex-wrap: wrap; gap: 10px; }
            h1 { margin: 0; font-size: 26px; color: #58a6ff; }
            h2 { font-size: 15px; color: #58a6ff; text-transform: uppercase; letter-spacing: 0.5px; margin: 0 0 12px 0; }
            .pipeline-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 15px; margin-bottom: 25px; }
            .pipeline-step { background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px; position: relative; }
            .step-number { position: absolute; top: -10px; left: 15px; background: #238636; color: white; font-size: 11px; font-weight: bold; padding: 2px 8px; border-radius: 10px; }
            .step-title { font-size: 14px; font-weight: bold; color: #58a6ff; margin-bottom: 8px; margin-top: 5px; text-transform: uppercase; letter-spacing: 0.5px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 20px; }
            .card { background-color: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
            .card h3 { margin-top: 0; color: #8b949e; font-size: 13px; text-transform: uppercase; letter-spacing: 1px; }
            .metric { font-size: 28px; font-weight: bold; color: #f0f6fc; margin: 10px 0 0 0; }
            .sub-text { font-size: 13px; color: #8b949e; margin-top: 6px; }
            .badge { display: inline-block; padding: 5px 12px; border-radius: 12px; font-size: 12px; font-weight: bold; background: __MODE_COLOR__; color: #fff; }
            .section { margin-top: 25px; }
            .symbol-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; }
            .symbol-card { background-color: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 15px; }
            .symbol-name { font-size: 15px; font-weight: bold; color: #58a6ff; }
            .symbol-qty { font-size: 20px; font-weight: bold; color: #f0f6fc; margin-top: 6px; }
            .symbol-detail { font-size: 12px; color: #8b949e; margin-top: 6px; }
            .symbol-pl { font-size: 14px; font-weight: bold; margin-top: 6px; }
            .health-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; }
            .health-item { background-color: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 12px 15px; }
            .health-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
            .health-value { font-size: 16px; font-weight: bold; color: #f0f6fc; margin-top: 4px; }
            table { width: 100%; border-collapse: collapse; font-size: 12px; }
            th { text-align: left; padding: 8px; color: #8b949e; border-bottom: 1px solid #30363d; text-transform: uppercase; font-size: 11px; }
            td { padding: 8px; border-bottom: 1px solid #21262d; color: #c9d1d9; font-family: monospace; }
            .history-wrap { max-height: 320px; overflow-y: auto; }
            canvas { width: 100%; height: 160px; display: block; }
            .log-box { background: #010409; border: 1px solid #30363d; padding: 12px; border-radius: 6px; font-family: monospace; font-size: 12px; height: 100px; overflow-y: auto; color: #7ee787; margin-top: 12px; }

            /* ===== FUTURISTIC THEME OVERRIDES ===== */
            :root { --cyan: #00e5ff; --magenta: #ff2bd6; --neon-green: #39ff88; --amber: #ffb800; --red: #ff3b5c; --panel: rgba(8, 18, 38, 0.62); --edge: rgba(0, 229, 255, 0.28); }
            html { background: #04060c; }
            body {
                background:
                    radial-gradient(ellipse at 15% -10%, rgba(0, 229, 255, 0.16), transparent 50%),
                    radial-gradient(ellipse at 90% 10%, rgba(255, 43, 214, 0.12), transparent 45%),
                    linear-gradient(rgba(0, 229, 255, 0.045) 1px, transparent 1px) 0 0 / 44px 44px,
                    linear-gradient(90deg, rgba(0, 229, 255, 0.045) 1px, transparent 1px) 0 0 / 44px 44px,
                    #04060c;
                background-attachment: fixed;
                color: #cfe8ff;
                font-family: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
                letter-spacing: 0.02em;
            }
            body::before {
                content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 50;
                background: repeating-linear-gradient(0deg, rgba(0, 0, 0, 0.16) 0px, rgba(0, 0, 0, 0.16) 1px, transparent 1px, transparent 3px);
                opacity: 0.35;
            }
            header { border-bottom: 1px solid var(--edge); box-shadow: 0 1px 0 rgba(0, 229, 255, 0.08), 0 12px 30px -20px var(--cyan); }
            h1 {
                font-size: 24px; text-transform: uppercase; letter-spacing: 0.22em;
                background: linear-gradient(90deg, var(--cyan), #9d7bff 55%, var(--magenta));
                -webkit-background-clip: text; background-clip: text; color: transparent;
                filter: drop-shadow(0 0 10px rgba(0, 229, 255, 0.45));
            }
            h2 { color: var(--cyan); letter-spacing: 0.2em; font-size: 12px; text-shadow: 0 0 8px rgba(0, 229, 255, 0.7); display: flex; align-items: center; gap: 8px; }
            h2::before { content: ""; width: 8px; height: 8px; background: var(--cyan); box-shadow: 0 0 10px var(--cyan); transform: rotate(45deg); }
            .card, .pipeline-step {
                background: var(--panel); border: 1px solid var(--edge); border-radius: 6px;
                backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
                box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.6), 0 8px 32px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.06), inset 0 0 40px rgba(0, 229, 255, 0.04);
                position: relative; animation: rise 0.6s ease both;
            }
            .card::after, .pipeline-step::after {
                content: ""; position: absolute; top: 0; left: 12px; right: 12px; height: 1px;
                background: linear-gradient(90deg, transparent, var(--cyan), transparent); opacity: 0.8;
            }
            .card h3 { color: rgba(0, 229, 255, 0.75); letter-spacing: 0.22em; font-size: 11px; }
            .metric { color: #fff; text-shadow: 0 0 14px rgba(0, 229, 255, 0.55); font-variant-numeric: tabular-nums; }
            .sub-text { color: #6f8aa8; }
            .step-number { background: linear-gradient(90deg, var(--cyan), var(--magenta)); color: #04060c; box-shadow: 0 0 12px rgba(0, 229, 255, 0.6); letter-spacing: 0.1em; }
            .step-title { color: var(--cyan); letter-spacing: 0.18em; text-shadow: 0 0 8px rgba(0, 229, 255, 0.5); }
            .badge { border: 1px solid rgba(255, 255, 255, 0.35); letter-spacing: 0.16em; box-shadow: 0 0 18px currentColor; animation: pulse 2.4s ease-in-out infinite; }
            .live-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; background: var(--neon-green); box-shadow: 0 0 10px var(--neon-green), 0 0 22px var(--neon-green); margin-right: 10px; vertical-align: middle; animation: blink 1.6s ease-in-out infinite; }
            #clock { font-size: 12px; color: var(--cyan); text-shadow: 0 0 8px rgba(0, 229, 255, 0.7); margin-right: 16px; letter-spacing: 0.15em; }
            .symbol-card, .health-item {
                background: linear-gradient(160deg, rgba(0, 229, 255, 0.07), rgba(255, 43, 214, 0.03));
                border: 1px solid rgba(0, 229, 255, 0.2); border-radius: 4px; transition: transform 0.2s, box-shadow 0.2s, border-color 0.2s;
            }
            .symbol-card:hover, .health-item:hover { transform: translateY(-2px); border-color: var(--cyan); box-shadow: 0 0 22px rgba(0, 229, 255, 0.25); }
            .symbol-name { color: var(--cyan); letter-spacing: 0.15em; text-shadow: 0 0 8px rgba(0, 229, 255, 0.6); }
            .health-label { color: #6f8aa8; letter-spacing: 0.18em; }
            .health-value { text-shadow: 0 0 10px rgba(255, 255, 255, 0.25); }
            th { color: var(--cyan); border-bottom: 1px solid var(--edge); letter-spacing: 0.15em; position: sticky; top: 0; background: rgba(4, 10, 24, 0.95); }
            td { border-bottom: 1px solid rgba(0, 229, 255, 0.08); }
            tbody tr { transition: background 0.15s; }
            tbody tr:hover { background: rgba(0, 229, 255, 0.07); }
            .log-box { background: rgba(0, 6, 4, 0.75); border: 1px solid rgba(57, 255, 136, 0.3); color: var(--neon-green); text-shadow: 0 0 6px rgba(57, 255, 136, 0.6); box-shadow: inset 0 0 24px rgba(57, 255, 136, 0.06); }
            canvas { filter: drop-shadow(0 0 6px rgba(0, 229, 255, 0.25)); }
            ::-webkit-scrollbar { width: 8px; height: 8px; }
            ::-webkit-scrollbar-track { background: rgba(0, 0, 0, 0.3); }
            ::-webkit-scrollbar-thumb { background: rgba(0, 229, 255, 0.35); border-radius: 4px; }
            @keyframes rise { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }
            @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
            @keyframes pulse { 0%, 100% { box-shadow: 0 0 10px currentColor; } 50% { box-shadow: 0 0 26px currentColor; } }
            select, .btn { background: rgba(0, 229, 255, 0.08); color: var(--cyan); border: 1px solid var(--edge); border-radius: 4px; padding: 7px 12px; font-family: inherit; font-size: 12px; letter-spacing: 0.1em; }
            .btn { cursor: pointer; text-transform: uppercase; transition: box-shadow 0.2s, background 0.2s; }
            .btn:hover { background: rgba(0, 229, 255, 0.2); box-shadow: 0 0 16px rgba(0, 229, 255, 0.45); }
            .btn:disabled { opacity: 0.5; cursor: wait; }
            .tag { display: inline-block; padding: 3px 10px; border-radius: 10px; font-size: 11px; letter-spacing: 0.12em; border: 1px solid currentColor; }
            .bt-controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
            .table-wrap { overflow-x: auto; }
            @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
        </style>
    </head>
    <body>
        <header>
            <div>
                <h1>AETHER Autonomous Trading Core</h1>
                <div style="font-size: 13px; color: #8b949e; margin-top: 4px;">Secure Crypto Telemetry & Pipeline Control Panel</div>
            </div>
            <div>
                <span id="clock">--:--:-- UTC</span>
                <span class="live-dot"></span><span id="sys-status" class="badge">__MODE_LABEL__</span>
            </div>
        </header>

        <div class="pipeline-container">
            <div class="pipeline-step">
                <span class="step-number">Step 1</span>
                <div class="step-title">The Watcher</div>
                <div style="font-size: 13px; color: #c9d1d9;">TradingView Webhook Listener</div>
                <div class="sub-text" style="margin-top: 10px;">Last Signal: <span id="pipeline-signal" style="color: #7ee787; font-weight: bold;">None</span></div>
            </div>
            <div class="pipeline-step">
                <span class="step-number">Step 2</span>
                <div class="step-title">Gateway</div>
                <div style="font-size: 13px; color: #c9d1d9;">Token Authentication Enforced</div>
                <div class="sub-text" style="margin-top: 10px;">Auth Status: <span style="color: #58a6ff; font-weight: bold;">Secured</span></div>
            </div>
            <div class="pipeline-step">
                <span class="step-number">Step 3</span>
                <div class="step-title">AETHER Brain</div>
                <div style="font-size: 13px; color: #c9d1d9;">Position-Aware Guard Engine</div>
                <div class="sub-text" style="margin-top: 10px;">Net Residual: <span id="pipeline-residual" style="color: #f0f6fc; font-weight: bold;">0.0</span></div>
            </div>
            <div class="pipeline-step">
                <span class="step-number">Step 4</span>
                <div class="step-title">Alpaca Broker</div>
                <div style="font-size: 13px; color: #c9d1d9;">Crypto Execution / Shadow Mode</div>
                <div class="sub-text" style="margin-top: 10px;">Live Trading: <span style="color: #7ee787; font-weight: bold;">__IS_LIVE_UPPER__</span></div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>Portfolio Value</h3>
                <div id="portfolio-value" class="metric">$0.00</div>
                <div class="sub-text">Buying Power: <span id="buying-power" style="color:#f0f6fc;">$0.00</span></div>
            </div>

            <div class="card">
                <h3>Cash Balance</h3>
                <div id="cash-balance" class="metric">$0.00</div>
                <div class="sub-text" id="balance-source">Alpaca Paper Sandbox</div>
            </div>

            <div class="card">
                <h3>Operating State</h3>
                <div id="op-state" class="metric" style="font-size: 22px; color: #58a6ff;">__MODE_LABEL__</div>
                <div class="sub-text">Risk Level: <span id="risk-level" style="color: #f0f6fc; font-weight: bold;">Medium</span></div>
            </div>

            <div class="card">
                <h3>Inventory Residual</h3>
                <div id="net-residual" class="metric">0.0</div>
                <div class="sub-text">Long Units: <span id="long-units">0</span> | Short Units: <span id="short-units">0</span></div>
            </div>
        </div>

        <div class="section card">
            <h2>System Health</h2>
            <div class="health-grid">
                <div class="health-item">
                    <div class="health-label">Uptime</div>
                    <div class="health-value" id="uptime">—</div>
                </div>
                <div class="health-item">
                    <div class="health-label">Alpaca Connection</div>
                    <div class="health-value" id="alpaca-status">—</div>
                </div>
                <div class="health-item">
                    <div class="health-label">Last Webhook Received</div>
                    <div class="health-value" id="last-webhook" style="font-size: 13px;">—</div>
                </div>
                <div class="health-item">
                    <div class="health-label">Last Execution</div>
                    <div class="health-value" id="last-execution" style="font-size: 13px;">—</div>
                </div>
                <div class="health-item">
                    <div class="health-label">VM Load (1/5/15m)</div>
                    <div class="health-value" id="vm-load" style="font-size: 14px;">-</div>
                </div>
                <div class="health-item">
                    <div class="health-label">VM Memory</div>
                    <div class="health-value" id="vm-mem">-</div>
                </div>
                <div class="health-item">
                    <div class="health-label">VM Disk</div>
                    <div class="health-value" id="vm-disk" style="font-size: 14px;">-</div>
                </div>
                <div class="health-item">
                    <div class="health-label">Telegram Alerts</div>
                    <div class="health-value" id="tg-status">-</div>
                </div>
            </div>
        </div>

        <div class="section card">
            <h2>Positions by Symbol</h2>
            <div id="symbol-cards" class="symbol-grid"></div>
        </div>

        <div class="section card">
            <h2>Strategy Engine <span id="strat-badge" class="tag" style="margin-left:auto;">LOADING</span></h2>
            <div class="sub-text" id="strat-meta">Loading strategy status...</div>
            <div class="table-wrap" style="margin-top:12px;">
                <table>
                    <thead>
                        <tr><th>Symbol</th><th>State</th><th>Price</th><th>EMA fast</th><th>EMA slow</th><th>Trend</th><th>RSI</th><th>Stop</th><th>Note</th></tr>
                    </thead>
                    <tbody id="strat-body">
                        <tr><td colspan="9" class="sub-text">Waiting for first cycle...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="section card">
            <h2>Strategy Backtest</h2>
            <div class="bt-controls">
                <select id="bt-symbol"><option>BTC/USD</option><option>ETH/USD</option><option>SOL/USD</option></select>
                <select id="bt-days"><option value="365">1 year</option><option value="730">2 years</option><option value="1460" selected>4 years</option></select>
                <button class="btn" id="bt-run" onclick="runBacktest()">Run backtest</button>
                <span class="sub-text" id="bt-status">Replays the live rules on real hourly history, including fees and slippage.</span>
            </div>
            <div id="bt-results" class="health-grid"></div>
            <canvas id="bt-chart" width="900" height="160" style="margin-top:14px;"></canvas>
            <div class="sub-text" id="bt-note"></div>
            <div class="table-wrap" style="margin-top:10px;">
                <table>
                    <thead><tr><th>Entry</th><th>Exit</th><th>Entry $</th><th>Exit $</th><th>P&amp;L</th><th>%</th><th>Reason</th></tr></thead>
                    <tbody id="bt-trades"></tbody>
                </table>
            </div>
        </div>

        <div class="section card">
            <h2>US ETF Momentum Engine <span id="us-badge" class="tag" style="margin-left:auto;">LOADING</span></h2>
            <div class="sub-text" id="us-meta">Loading...</div>
            <div id="us-tiles" class="health-grid" style="margin-top:12px;"></div>
            <div class="table-wrap" style="margin-top:12px;">
                <table>
                    <thead><tr><th>ETF</th><th>Momentum</th><th>Rank</th><th>Target</th><th>Holding</th><th>Value</th></tr></thead>
                    <tbody id="us-body"></tbody>
                </table>
            </div>
            <canvas id="us-chart" width="900" height="160" style="margin-top:14px;"></canvas>
            <div class="sub-text" id="us-note"></div>
            <div class="table-wrap" style="margin-top:10px;">
                <table>
                    <thead><tr><th>Rebalance</th><th>Signals as of</th><th>Holding</th><th>Orders</th><th>Equity</th></tr></thead>
                    <tbody id="us-history"></tbody>
                </table>
            </div>
        </div>

        <div class="section card">
            <h2>Portfolio Value Trend</h2>
            <canvas id="portfolio-chart" width="900" height="160"></canvas>
            <div class="sub-text" id="chart-note">Collecting data points every 30 seconds while this dashboard is open...</div>
        </div>

        <div class="section card">
            <h2>Trade History</h2>
            <div class="history-wrap">
                <table>
                    <thead>
                        <tr><th>Time (UTC)</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Detail</th></tr>
                    </thead>
                    <tbody id="history-body">
                        <tr><td colspan="7" class="sub-text">No trades recorded yet.</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <div class="section card">
            <h2>Client Telemetry Log</h2>
            <div id="test-log" class="log-box">Dashboard initialized securely. Awaiting telemetry...</div>
        </div>

        <script>
            const urlParams = new URLSearchParams(window.location.search);
            const secretToken = urlParams.get('secret') || '';
            const qs = '';  // auth is the session cookie now
            const _origFetch = window.fetch.bind(window);
            window.fetch = async (...args) => {
                const r = await _origFetch(...args);
                if (r.status === 401) window.location.href = '/login';
                return r;
            };

            function logLine(msg) {
                const box = document.getElementById('test-log');
                const ts = new Date().toLocaleTimeString();
                box.innerHTML += '<div>[' + ts + '] ' + msg + '</div>';
                box.scrollTop = box.scrollHeight;
                while (box.children.length > 50) box.removeChild(box.firstChild);
            }

            function fmtMoney(v) {
                if (v === null || v === undefined || v === '') return 'N/A';
                const n = parseFloat(v);
                return (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            }

            function formatUptime(seconds) {
                if (!seconds) return '0m';
                const h = Math.floor(seconds / 3600);
                const m = Math.floor((seconds % 3600) / 60);
                return h + 'h ' + m + 'm';
            }

            function renderSymbolCards(positions) {
                const container = document.getElementById('symbol-cards');
                if (!positions || positions.length === 0) {
                    container.innerHTML = '<div class="sub-text">No tracked symbols.</div>';
                    return;
                }
                container.innerHTML = positions.map(function (p) {
                    const flat = !p.qty || Math.abs(p.qty) < 0.00000001;
                    let plHtml = '';
                    if (!flat && p.unrealized_pl !== null && p.unrealized_pl !== undefined) {
                        const plColor = p.unrealized_pl >= 0 ? '#3fb950' : '#f85149';
                        const pct = p.unrealized_plpc !== null && p.unrealized_plpc !== undefined ? (p.unrealized_plpc * 100).toFixed(2) : '0.00';
                        plHtml = '<div class="symbol-pl" style="color:' + plColor + '">' + fmtMoney(p.unrealized_pl) + ' (' + pct + '%)</div>';
                    }
                    const detailHtml = flat ? '<div class="symbol-detail">No open position</div>' :
                        '<div class="symbol-detail">Entry: ' + fmtMoney(p.avg_entry_price) + ' &middot; Now: ' + fmtMoney(p.current_price) + '</div>';
                    return '<div class="symbol-card">' +
                        '<div class="symbol-name">' + p.symbol + '</div>' +
                        '<div class="symbol-qty">' + (flat ? '—' : Number(p.qty).toFixed(4) + ' units') + '</div>' +
                        detailHtml + plHtml +
                        '</div>';
                }).join('');
            }

            function renderHistory(rows) {
                const tbody = document.getElementById('history-body');
                if (!rows || rows.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="7" class="sub-text">No trades recorded yet.</td></tr>';
                    return;
                }
                const statusColors = {success: '#3fb950', shadow_recorded: '#d29922', rejected: '#f85149', error: '#f85149'};
                tbody.innerHTML = rows.slice(0, 50).map(function (r) {
                    const color = statusColors[r.status] || '#8b949e';
                    return '<tr>' +
                        '<td>' + r.timestamp + '</td>' +
                        '<td>' + r.symbol + '</td>' +
                        '<td>' + r.side + '</td>' +
                        '<td>' + r.executed_qty + '</td>' +
                        '<td>' + fmtMoney(r.price) + '</td>' +
                        '<td style="color:' + color + '">' + r.status + '</td>' +
                        '<td>' + (r.detail || '') + '</td>' +
                        '</tr>';
                }).join('');
            }

            function drawChart(points, canvasId, noteId) {
                const canvas = document.getElementById(canvasId || 'portfolio-chart');
                const ctx = canvas.getContext('2d');
                const w = canvas.width, h = canvas.height;
                ctx.clearRect(0, 0, w, h);
                if (!points || points.length < 2) {
                    ctx.fillStyle = '#8b949e';
                    ctx.font = '13px sans-serif';
                    ctx.fillText('Collecting data points...', 10, h / 2);
                    return;
                }
                const values = points.map(function (p) { return p.v; });
                const min = Math.min.apply(null, values);
                const max = Math.max.apply(null, values);
                const range = (max - min) || 1;
                const pad = 12;
                ctx.strokeStyle = 'rgba(0, 229, 255, 0.08)';
                ctx.lineWidth = 1;
                for (let g = 1; g < 5; g++) {
                    const gy = pad + (g / 5) * (h - pad * 2);
                    ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
                }
                const xy = points.map(function (p, i) {
                    return [pad + (i / (points.length - 1)) * (w - pad * 2), h - pad - ((p.v - min) / range) * (h - pad * 2)];
                });
                const fill = ctx.createLinearGradient(0, 0, 0, h);
                fill.addColorStop(0, 'rgba(0, 229, 255, 0.35)');
                fill.addColorStop(1, 'rgba(0, 229, 255, 0)');
                ctx.beginPath();
                ctx.moveTo(xy[0][0], h);
                xy.forEach(function (c) { ctx.lineTo(c[0], c[1]); });
                ctx.lineTo(xy[xy.length - 1][0], h);
                ctx.closePath();
                ctx.fillStyle = fill;
                ctx.fill();
                const stroke = ctx.createLinearGradient(0, 0, w, 0);
                stroke.addColorStop(0, '#00e5ff');
                stroke.addColorStop(1, '#ff2bd6');
                ctx.beginPath();
                xy.forEach(function (c, i) { if (i === 0) ctx.moveTo(c[0], c[1]); else ctx.lineTo(c[0], c[1]); });
                ctx.strokeStyle = stroke;
                ctx.lineWidth = 2.5;
                ctx.shadowColor = '#00e5ff';
                ctx.shadowBlur = 12;
                ctx.stroke();
                const last = xy[xy.length - 1];
                ctx.beginPath();
                ctx.arc(last[0], last[1], 4, 0, Math.PI * 2);
                ctx.fillStyle = '#ffffff';
                ctx.shadowColor = '#ff2bd6';
                ctx.shadowBlur = 18;
                ctx.fill();
                ctx.shadowBlur = 0;
                ctx.fillStyle = '#6f8aa8';
                ctx.font = '11px monospace';
                ctx.fillText(fmtMoney(max), 4, 12);
                ctx.fillText(fmtMoney(min), 4, h - 4);
                document.getElementById(noteId || 'chart-note').innerText = points.length + ' data points' + (noteId ? '.' : ' collected.');
            }

            async function updateSnapshot() {
                try {
                    const res = await fetch('/api/snapshot?' + qs);
                    if (!res.ok) throw new Error('Snapshot request failed (' + res.status + ')');
                    const data = await res.json();

                    const bal = data.balances || {};
                    document.getElementById('portfolio-value').innerText = fmtMoney(bal.portfolio_value);
                    document.getElementById('buying-power').innerText = fmtMoney(bal.buying_power);
                    document.getElementById('cash-balance').innerText = fmtMoney(bal.cash);
                    document.getElementById('balance-source').innerText = 'Source: ' + (bal.source || 'unknown');

                    document.getElementById('op-state').innerText = data.operating_state;
                    document.getElementById('risk-level').innerText = data.risk_level;

                    document.getElementById('net-residual').innerText = data.inventory_state.net_residual.toFixed(4);
                    document.getElementById('long-units').innerText = data.inventory_state.long_units;
                    document.getElementById('short-units').innerText = data.inventory_state.short_units;
                    document.getElementById('pipeline-residual').innerText = data.inventory_state.net_residual.toFixed(4);
                    if (data.last_signal) {
                        document.getElementById('pipeline-signal').innerText = data.last_signal;
                    }

                    const health = data.health || {};
                    document.getElementById('uptime').innerText = formatUptime(health.uptime_seconds);
                    const connColor = health.alpaca_source === 'live' ? '#3fb950' : '#f85149';
                    const statusEl = document.getElementById('alpaca-status');
                    statusEl.innerText = health.alpaca_source || 'unknown';
                    statusEl.style.color = connColor;
                    document.getElementById('last-webhook').innerText = health.last_webhook_received_at || 'Never';
                    document.getElementById('last-execution').innerText = health.last_execution_time || 'Never';
                    const vm = health.vm || {};
                    document.getElementById('vm-load').innerText = vm.load1 === undefined ? 'n/a' : vm.load1.toFixed(2) + ' / ' + vm.load5.toFixed(2) + ' / ' + vm.load15.toFixed(2);
                    const memEl = document.getElementById('vm-mem');
                    memEl.innerText = vm.mem_used_pct === undefined ? 'n/a' : vm.mem_used_pct + '%';
                    memEl.style.color = vm.mem_used_pct >= 90 ? '#ff3b5c' : '#f0f6fc';
                    const diskEl = document.getElementById('vm-disk');
                    diskEl.innerText = vm.disk_used_pct === undefined ? 'n/a' : vm.disk_used_pct + '% (' + vm.disk_free_gb + ' GB free)';
                    diskEl.style.color = vm.disk_used_pct >= 85 ? '#ff3b5c' : '#f0f6fc';
                    const tgEl = document.getElementById('tg-status');
                    tgEl.innerText = health.telegram ? 'ON' : 'OFF';
                    tgEl.style.color = health.telegram ? '#39ff88' : '#6f8aa8';

                    renderSymbolCards(data.positions_detail);

                    logLine('Snapshot OK — residual ' + data.inventory_state.net_residual.toFixed(4));
                } catch (err) {
                    logLine('ERROR (snapshot): ' + err.message);
                }
            }

            async function updateHistory() {
                try {
                    const res = await fetch('/api/history?' + qs);
                    if (!res.ok) throw new Error('History request failed (' + res.status + ')');
                    const data = await res.json();
                    renderHistory(data.trade_history || []);
                } catch (err) {
                    logLine('ERROR (history): ' + err.message);
                }
            }

            async function updateChart() {
                try {
                    const res = await fetch('/api/portfolio-history?' + qs);
                    if (!res.ok) throw new Error('Chart request failed (' + res.status + ')');
                    const data = await res.json();
                    drawChart(data.portfolio_history || []);
                } catch (err) {
                    logLine('ERROR (chart): ' + err.message);
                }
            }

            function fmtNum(v, d) {
                return (v === null || v === undefined) ? '—' : Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
            }

            function renderStrategy(s) {
                const badge = document.getElementById('strat-badge');
                if (!s.enabled) {
                    badge.innerText = 'DISABLED'; badge.style.color = '#6f8aa8';
                } else if (s.kill_switch) {
                    badge.innerText = 'KILL-SWITCH'; badge.style.color = '#ff3b5c';
                } else {
                    badge.innerText = s.mode + ' · ACTIVE'; badge.style.color = s.mode === 'LIVE' ? '#39ff88' : '#ffb800';
                }
                const p = s.params || {};
                document.getElementById('strat-meta').innerHTML = s.enabled
                    ? 'EMA ' + p.fast + '/' + p.slow + ' cross &middot; trend EMA ' + p.trend + ' &middot; RSI &lt; ' + p.rsi_max +
                      ' &middot; stop ' + p.atr_mult + '&times;ATR &middot; risk ' + (p.risk_pct * 100) + '%/trade &middot; daily loss limit ' + s.max_daily_loss_pct + '%' +
                      ' &middot; last cycle: ' + (s.last_cycle || 'pending') + (s.last_error ? ' &middot; <span style="color:#ff3b5c">error: ' + s.last_error + '</span>' : '')
                    : 'Engine is off. Set STRATEGY_ENABLED=true in .env on the server and restart to enable. Backtests below still work.';
                const body = document.getElementById('strat-body');
                if (!s.symbols || s.symbols.length === 0) {
                    body.innerHTML = '<tr><td colspan="9" class="sub-text">' + (s.enabled ? 'Waiting for first cycle...' : 'Strategy engine disabled.') + '</td></tr>';
                    return;
                }
                const colors = {LONG: '#39ff88', FLAT: '#6f8aa8', WARMUP: '#ffb800', ERROR: '#ff3b5c'};
                body.innerHTML = s.symbols.map(function (r) {
                    const trend = r.trend_ok === undefined ? '—' : (r.trend_ok ? '<span style="color:#39ff88">UP</span>' : '<span style="color:#ff3b5c">DOWN</span>');
                    return '<tr><td>' + r.symbol + '</td>' +
                        '<td style="color:' + (colors[r.state] || '#c9d1d9') + '">' + r.state + '</td>' +
                        '<td>' + fmtMoney(r.price) + '</td>' +
                        '<td>' + fmtNum(r.ema_fast, 2) + '</td><td>' + fmtNum(r.ema_slow, 2) + '</td>' +
                        '<td>' + trend + '</td><td>' + fmtNum(r.rsi, 1) + '</td>' +
                        '<td>' + (r.stop ? fmtMoney(r.stop) : '—') + '</td>' +
                        '<td>' + (r.note || '') + '</td></tr>';
                }).join('');
            }

            async function updateStrategy() {
                try {
                    const res = await fetch('/api/strategy?' + qs);
                    if (!res.ok) throw new Error('Strategy request failed (' + res.status + ')');
                    renderStrategy(await res.json());
                } catch (err) {
                    logLine('ERROR (strategy): ' + err.message);
                }
            }

            async function runBacktest() {
                const btn = document.getElementById('bt-run');
                const status = document.getElementById('bt-status');
                btn.disabled = true;
                status.innerText = 'Fetching history and simulating... this can take up to a minute for 4 years.';
                try {
                    const sym = document.getElementById('bt-symbol').value;
                    const days = document.getElementById('bt-days').value;
                    const res = await fetch('/api/backtest?symbol=' + encodeURIComponent(sym) + '&days=' + days + '&' + qs);
                    const r = await res.json();
                    if (!res.ok) throw new Error(r.detail || ('HTTP ' + res.status));
                    const good = function (v) { return v >= 0 ? '#39ff88' : '#ff3b5c'; };
                    const tile = function (label, value, color) {
                        return '<div class="health-item"><div class="health-label">' + label + '</div><div class="health-value" style="color:' + (color || '#f0f6fc') + '">' + value + '</div></div>';
                    };
                    document.getElementById('bt-results').innerHTML =
                        tile('Strategy return', r.total_return_pct + '%', good(r.total_return_pct)) +
                        tile('Buy &amp; hold', r.buy_hold_return_pct + '%', good(r.buy_hold_return_pct)) +
                        tile('Max drawdown', r.max_drawdown_pct + '%', '#ffb800') +
                        tile('Trades', r.trades) +
                        tile('Win rate', r.win_rate_pct + '%') +
                        tile('Profit factor', r.profit_factor === null ? '—' : r.profit_factor) +
                        tile('Fees paid', fmtMoney(r.fees_paid));
                    drawChart(r.equity_curve, 'bt-chart', 'bt-note');
                    document.getElementById('bt-trades').innerHTML = (r.recent_trades || []).map(function (t) {
                        return '<tr><td>' + t.entry_t.substr(0, 16).replace('T', ' ') + '</td><td>' + t.exit_t.substr(0, 16).replace('T', ' ') + '</td>' +
                            '<td>' + fmtMoney(t.entry) + '</td><td>' + fmtMoney(t.exit) + '</td>' +
                            '<td style="color:' + good(t.pnl) + '">' + fmtMoney(t.pnl) + '</td><td style="color:' + good(t.pnl) + '">' + t.pnl_pct + '%</td><td>' + t.reason + '</td></tr>';
                    }).join('');
                    status.innerText = sym + ' · ' + r.period_start.substr(0, 10) + ' to ' + r.period_end.substr(0, 10) + ' · ' + r.bars + ' hourly bars · past results do not predict future returns.';
                } catch (err) {
                    status.innerText = 'Backtest failed: ' + err.message;
                }
                btn.disabled = false;
            }

            function renderUS(d) {
                const badge = document.getElementById('us-badge');
                if (!d.enabled) {
                    badge.innerText = 'DISABLED'; badge.style.color = '#6f8aa8';
                    document.getElementById('us-meta').innerText = 'Set US_ENABLED=true in .env on the server and restart to enable.';
                    return;
                }
                badge.innerText = d.mode; badge.style.color = d.mode === 'PAPER' ? '#39ff88' : '#ffb800';
                document.getElementById('us-meta').innerHTML = 'Top 3 of 10 ETFs by 3/6/12-month momentum, cash filter, monthly rebalance at ~15:30 ET (about 23:30 UAE in summer). Allocation ' +
                    fmtMoney(d.allocation_usd) + ' &middot; ' + (d.note || '') + (d.last_error ? ' &middot; <span style="color:#ff3b5c">' + d.last_error + '</span>' : '');
                const tile = function (label, value) {
                    return '<div class="health-item"><div class="health-label">' + label + '</div><div class="health-value" style="font-size:14px;">' + value + '</div></div>';
                };
                document.getElementById('us-tiles').innerHTML =
                    tile('Equity', d.equity === null ? 'not invested yet' : fmtMoney(d.equity)) +
                    tile('Cash', d.cash === null ? '-' : fmtMoney(d.cash)) +
                    tile('Last rebalance', d.last_rebalance_month || 'never') +
                    tile('Signals as of', d.signal_date || (d.preview ? d.preview.signal_date : '-'));
                let rows = d.rows || [];
                if (rows.every(function (r) { return r.score === null; }) && d.preview) {
                    rows = rows.map(function (r) {
                        const sc = d.preview.scores[r.symbol];
                        return Object.assign({}, r, {score: sc === undefined ? null : sc, target: d.preview.weights[r.symbol] || 0});
                    });
                }
                rows = rows.slice().sort(function (a, b) { return (a.rank || 99) - (b.rank || 99); });
                document.getElementById('us-body').innerHTML = rows.map(function (r) {
                    const sc = r.score === null ? '-' : (r.score * 100).toFixed(1) + '%';
                    const col = r.score === null ? '#c9d1d9' : (r.score >= 0 ? '#39ff88' : '#ff3b5c');
                    return '<tr><td>' + r.symbol + '</td><td style="color:' + col + '">' + sc + '</td><td>' + (r.rank || '-') + '</td>' +
                        '<td>' + (r.target ? (r.target * 100).toFixed(0) + '%' : '-') + '</td>' +
                        '<td>' + (r.qty ? r.qty.toFixed(4) : '-') + '</td><td>' + (r.value ? fmtMoney(r.value) : '-') + '</td></tr>';
                }).join('');
                const pts = (d.equity_history || []).map(function (p) { return {v: p.v}; });
                drawChart(pts, 'us-chart', 'us-note');
                if (pts.length < 2) document.getElementById('us-note').innerText = 'Equity curve appears after two market closes with the engine invested.';
                document.getElementById('us-history').innerHTML = (d.history || []).map(function (h) {
                    const w = Object.keys(h.weights).sort().map(function (k) { return k + ' ' + (h.weights[k] * 100).toFixed(0) + '%'; }).join(', ');
                    return '<tr><td>' + h.date + (h.complete ? '' : ' (incomplete)') + '</td><td>' + h.signal_date + '</td><td>' + w + '</td><td>' + h.orders.length + '</td><td>' + fmtMoney(h.equity) + '</td></tr>';
                }).join('') || '<tr><td colspan="5" class="sub-text">No rebalances yet.</td></tr>';
            }

            async function updateUS() {
                try {
                    const res = await fetch('/api/us?' + qs);
                    if (!res.ok) throw new Error('US engine request failed (' + res.status + ')');
                    renderUS(await res.json());
                } catch (err) {
                    logLine('ERROR (us): ' + err.message);
                }
            }

            function tickClock() {
                document.getElementById('clock').innerText = new Date().toISOString().substr(11, 8) + ' UTC';
            }
            tickClock();
            setInterval(tickClock, 1000);

            updateSnapshot();
            updateHistory();
            updateChart();
            updateStrategy();
            setInterval(updateStrategy, 10000);
            updateUS();
            setInterval(updateUS, 30000);
            setInterval(updateSnapshot, 5000);
            setInterval(updateHistory, 15000);
            setInterval(updateChart, 30000);
        </script>
    </body>
    </html>
    """

    html = (
        html.replace("__MODE_COLOR__", mode_color)
        .replace("__MODE_LABEL__", mode_label)
        .replace("__IS_LIVE_UPPER__", str(is_live).upper())
    )
    return html


@app.post("/webhook/tradingview")
async def tradingview_webhook(alert: TradeAlertPayload, x_webhook_secret: str = Header(default="")):
    """Secure webhook endpoint verifying the token and enforcing position-aware execution guards.
    Every attempt — successful, shadow-recorded, rejected, or errored — is recorded to the
    trade history log so the dashboard reflects what AETHER actually did, not just its
    successful trades."""
    verify_access(x_webhook_secret=x_webhook_secret or alert.secret)

    with STATE_LOCK:
        state = load_state()
        state["last_webhook_received_at"] = utcnow_str()
        save_state(state)

    symbol = normalize_symbol(alert.symbol)
    raw_condition = alert.condition.upper()
    side = "BUY" if "BUY" in raw_condition else "SELL" if "SELL" in raw_condition else None

    if symbol is None or side is None:
        reason = (
            f"Symbol '{alert.symbol}' is not a tracked/tradable pair."
            if symbol is None
            else f"Unrecognized condition '{alert.condition}'."
        )
        with STATE_LOCK:
            state = load_state()
            record_trade_history(
                state,
                symbol=symbol or alert.symbol,
                side=side or "UNKNOWN",
                requested_qty=0.0,
                executed_qty=0.0,
                price=alert.price,
                result_status="rejected",
                detail=reason,
            )
            save_state(state)
        raise HTTPException(status_code=400, detail=reason)

    with STATE_LOCK:
        state = load_state()

        local_available = state["positions"].get(symbol, {"long": 0.0}).get("long", 0.0)
        # Prefer the broker's real position as the source of truth for the naked-short
        # guard. Only fall back to the locally tracked figure when the broker can't be
        # reached (no credentials configured, or a transient API failure) — a guard
        # whose whole purpose is preventing naked shorts must not rely solely on a
        # local file that can drift after a crash or a manual trade.
        live_qty = broker.get_position_qty(symbol)
        available_long = live_qty if live_qty is not None else local_available
        is_closing = side == "SELL" and available_long > 0

        if side == "BUY":
            balances = broker.get_account_balance()
            equity = float(balances["portfolio_value"]) if balances.get("source") == "live" else STRATEGY_SHADOW_EQUITY
            order_qty = round(TRADE_NOTIONAL_USD / alert.price, 6)
            order_value = order_qty * alert.price
            if order_qty <= 0 or order_value > equity * MAX_ORDER_PCT:
                reason = (f"Order size ${order_value:,.2f} exceeds the per-order cap of "
                          f"{MAX_ORDER_PCT * 100:g}% of equity (${equity * MAX_ORDER_PCT:,.2f}).")
                record_trade_history(state, symbol=symbol, side=side, requested_qty=order_qty,
                                     executed_qty=0.0, price=alert.price,
                                     result_status="rejected", detail=reason)
                save_state(state)
                raise HTTPException(status_code=400, detail=reason)
        else:
            order_qty = available_long

        execution_result = broker.execute_order(
            symbol=symbol,
            qty=order_qty,
            side=side,
            limit_price=alert.price,
            is_closing=is_closing,
            available_long=available_long,
        )

        result_status = execution_result.get("status")
        executed_qty = execution_result.get("qty", 0.0)
        detail = execution_result.get("reason") or execution_result.get("message") or execution_result.get("order_id")

        record_trade_history(
            state, symbol=symbol, side=side, requested_qty=order_qty,
            executed_qty=executed_qty, price=alert.price,
            result_status=result_status, detail=detail,
        )

        if result_status in ("success", "shadow_recorded"):
            pos = state["positions"].setdefault(symbol, {"long": 0.0, "short": 0.0})
            if side == "BUY":
                pos["long"] += executed_qty
            else:
                close_amount = min(executed_qty, pos["long"])
                remaining_short = executed_qty - close_amount
                pos["long"] -= close_amount
                pos["short"] += remaining_short
            state["last_signal"] = f"{symbol} ({side})"
            state["last_execution_time"] = utcnow_str()

        save_state(state)
        position_snapshot = dict(state["positions"].get(symbol, {"long": 0.0, "short": 0.0}))

    if result_status == "rejected":
        raise HTTPException(status_code=400, detail=execution_result.get("reason"))
    if result_status == "error":
        raise HTTPException(status_code=502, detail=execution_result.get("message", "Broker execution failed."))

    logger.info(f"Processed secure crypto alert for {symbol}. Position: {position_snapshot}")

    return {
        "status": "executed",
        "symbol": symbol,
        "broker_response": execution_result,
        "position": position_snapshot,
    }


@app.get("/account")
async def account_telemetry(secret: str = Query(default="")):
    """Returns authenticated real-time account balances and aggregate inventory state."""
    pass  # auth: _dashboard_auth session middleware
    with STATE_LOCK:
        state = load_state()
    balances = broker.get_account_balance()
    long_units = sum(p.get("long", 0.0) for p in state["positions"].values())
    short_units = sum(p.get("short", 0.0) for p in state["positions"].values())
    return {
        "balances": balances,
        "inventory_state": {
            "long_units": long_units,
            "short_units": short_units,
            "net_residual": long_units - short_units,
        },
    }


@app.get("/api/stocks")
async def get_tracked_stocks(secret: str = Query(default="")):
    """Returns active crypto trading pairs monitored by AETHER."""
    pass  # auth: _dashboard_auth session middleware
    return {"status": "success", "tracked_symbols": sorted(TRACKED_SYMBOLS)}


@app.get("/api/history")
async def get_trade_history(secret: str = Query(default="")):
    """Returns the persisted trade history log, newest entries first."""
    pass  # auth: _dashboard_auth session middleware
    with STATE_LOCK:
        state = load_state()
    return {"status": "success", "trade_history": list(reversed(state.get("trade_history", [])))}


@app.get("/api/portfolio-history")
async def get_portfolio_history(secret: str = Query(default="")):
    """Returns the sampled portfolio-value time series backing the dashboard chart."""
    pass  # auth: _dashboard_auth session middleware
    with STATE_LOCK:
        state = load_state()
    return {"status": "success", "portfolio_history": state.get("portfolio_history", [])}


@app.get("/api/snapshot")
async def get_system_snapshot(secret: str = Query(default="")):
    """Returns a comprehensive portfolio and system state snapshot for the dashboard,
    including a per-symbol position breakdown sourced from the broker's real position
    data (avg entry price, current price, unrealized P&L) and a system health summary.
    Also opportunistically samples the current portfolio value into the persisted
    chart history, throttled to once per PORTFOLIO_SAMPLE_INTERVAL_SECONDS so a
    dashboard polling every 5 seconds doesn't flood the history with redundant points."""
    pass  # auth: _dashboard_auth session middleware
    balances = broker.get_account_balance()
    broker_positions = broker.get_open_positions()
    is_live = os.getenv("LIVE_TRADING", "false").lower() == "true"

    broker_positions_by_symbol = {}
    for p in broker_positions:
        norm = p.symbol.replace("/", "").upper()
        for sym in TRACKED_SYMBOLS:
            if sym.replace("/", "") == norm:
                broker_positions_by_symbol[sym] = p
                break

    with STATE_LOCK:
        state = load_state()

        positions_detail = []
        for sym in sorted(TRACKED_SYMBOLS):
            p = broker_positions_by_symbol.get(sym)
            local = state["positions"].get(sym, {"long": 0.0, "short": 0.0})
            if p is not None:
                positions_detail.append({
                    "symbol": sym,
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price) if p.avg_entry_price is not None else None,
                    "current_price": float(p.current_price) if p.current_price is not None else None,
                    "market_value": float(p.market_value) if p.market_value is not None else None,
                    "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl is not None else None,
                    "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc is not None else None,
                    "source": "live",
                })
            else:
                positions_detail.append({
                    "symbol": sym,
                    "qty": local.get("long", 0.0) - local.get("short", 0.0),
                    "avg_entry_price": None,
                    "current_price": None,
                    "market_value": None,
                    "unrealized_pl": None,
                    "unrealized_plpc": None,
                    "source": "local_tracked" if (local.get("long") or local.get("short")) else "flat",
                })

        now = datetime.now(timezone.utc)
        history = state.get("portfolio_history", [])
        portfolio_value = balances.get("portfolio_value")
        should_sample = portfolio_value is not None and balances.get("source") == "live" and (
            not history
            or (now - datetime.fromisoformat(history[-1]["t"])).total_seconds() >= PORTFOLIO_SAMPLE_INTERVAL_SECONDS
        )
        if should_sample:
            try:
                history.append({"t": now.isoformat(), "v": float(portfolio_value)})
                state["portfolio_history"] = history[-MAX_PORTFOLIO_POINTS:]
                save_state(state)
            except (TypeError, ValueError):
                pass

        long_units = sum(p.get("long", 0.0) for p in state["positions"].values())
        short_units = sum(p.get("short", 0.0) for p in state["positions"].values())

        response = {
            "status": "success",
            "engine": "AETHER Autonomous Core",
            "operating_state": "Live Execution" if is_live else "Observation (Shadow Recorder)",
            "risk_level": state.get("risk_level", "Medium"),
            "loss_used_pct": state.get("loss_used_pct", 0.0),
            "active_assets_count": len(broker_positions) if broker_positions else 0,
            "balances": balances,
            "positions_detail": positions_detail,
            "inventory_state": {
                "long_units": long_units,
                "short_units": short_units,
                "net_residual": long_units - short_units,
            },
            "health": {
                "uptime_seconds": (now - STARTED_AT).total_seconds(),
                "alpaca_source": balances.get("source"),
                "live_trading": is_live,
                "last_webhook_received_at": state.get("last_webhook_received_at"),
                "last_execution_time": state.get("last_execution_time"),
                "vm": notify.vm_metrics(),
                "telegram": notify.enabled(),
                "services": {k.split("|")[0]: v for k, v in service_states().items()},
            },
            "last_signal": state.get("last_signal", "None"),
            "active_grid": state.get("active_grid", {}),
        }

    return response


# New dashboard at /, old one kept at /classic.
import dashboard_v2 as _dashboard_v2  # noqa: E402
app.include_router(_dashboard_v2.router)
