"""AETHER dashboard v2.

Adds two routes to the existing FastAPI app:
  GET /              the new dashboard page (dashboard_v2.html, re-read on every request)
  GET /api/overview  one JSON document with money, positions, trades, history and health

Read-only by design: it only sends GET requests to Alpaca and never places,
changes or cancels orders. Login is enforced by the _dashboard_auth middleware
in aether_engine.py, which covers every path not explicitly public.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

import notify

router = APIRouter()

_PAGE = Path(__file__).with_name("dashboard_v2.html")
_STARTED = time.time()
_CACHE = {"t": 0.0, "data": None}
_LOCK = threading.Lock()
CACHE_SECONDS = 20
HTTP_TIMEOUT = 10


# ---------- helpers ----------

def _env(*names):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


def _flag(name, default="false"):
    return os.getenv(name, default).strip().lower() == "true"


def _is_live():
    return _flag("LIVE_TRADING")


def _base_url():
    return "https://api.alpaca.markets" if _is_live() else "https://paper-api.alpaca.markets"


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


DATA_URL = "https://data.alpaca.markets"


def _get(path, params=None, base=None):
    key = _env("APCA_API_KEY_ID", "ALPACA_API_KEY", "ALPACA_KEY_ID")
    sec = _env("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET")
    if not key or not sec:
        raise RuntimeError("Alpaca API keys are not in the environment")
    url = (base or _base_url()) + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Alpaca {path} returned HTTP {e.code}") from None


def _pretty(sym, asset_class=""):
    """BTCUSD -> BTC/USD for crypto; stock and ETF tickers unchanged."""
    sym = sym or ""
    if "/" in sym:
        return sym
    if asset_class == "crypto" or (asset_class == "" and sym.endswith("USD") and len(sym) >= 6):
        return sym[:-3] + "/USD"
    return sym


def _engine(asset_class):
    return {"crypto": "Crypto trend", "us_equity": "US ETFs"}.get(asset_class, asset_class or "Other")


# ---------- data from Alpaca ----------

def _positions():
    out = []
    for p in _get("/v2/positions"):
        ac = p.get("asset_class", "")
        out.append({
            "symbol": _pretty(p.get("symbol"), ac),
            "engine": _engine(ac),
            "side": p.get("side", "long"),
            "qty": _f(p.get("qty")),
            "avg_entry": _f(p.get("avg_entry_price")),
            "price": _f(p.get("current_price")),
            "cost": _f(p.get("cost_basis")),
            "value": _f(p.get("market_value")),
            "upl": _f(p.get("unrealized_pl")),
            "upl_pct": _f(p.get("unrealized_plpc")) * 100,
            "today_pl": _f(p.get("unrealized_intraday_pl")),
        })
    out.sort(key=lambda x: -abs(x["value"]))
    return out


def _history(period, timeframe):
    params = {"period": period, "timeframe": timeframe}
    if timeframe != "1D":
        params["extended_hours"] = "true"
    h = _get("/v2/account/portfolio/history", params)
    ts, eq = h.get("timestamp") or [], h.get("equity") or []
    # Days before the account existed come back as 0 or null; drop them.
    return [[int(t), float(e)] for t, e in zip(ts, eq) if e]


def _fills():
    fills, token = [], None
    for _ in range(50):  # up to 5,000 fills
        params = {"direction": "asc", "page_size": 100}
        if token:
            params["page_token"] = token
        page = _get("/v2/account/activities/FILL", params)
        if not page:
            break
        fills.extend(page)
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return fills


def _merge_by_order(fills):
    """Partial fills of the same order become one row with a volume-weighted price."""
    merged, idx = [], {}
    for a in fills:
        oid = a.get("order_id") or a.get("id")
        q, px = _f(a.get("qty")), _f(a.get("price"))
        if q <= 0:
            continue
        if oid in idx:
            m = merged[idx[oid]]
            m["px"] = (m["px"] * m["q"] + px * q) / (m["q"] + q)
            m["q"] += q
            m["t"] = a.get("transaction_time") or m["t"]
        else:
            idx[oid] = len(merged)
            merged.append({"symbol": _pretty(a.get("symbol")), "side": a.get("side", ""),
                           "q": q, "px": px, "t": a.get("transaction_time", "")})
    return merged


def _closed_trades(orders):
    """First-in, first-out matching of sells against earlier buys (long positions only)."""
    lots = defaultdict(deque)
    closed = []
    for o in orders:
        sym, q, px = o["symbol"], o["q"], o["px"]
        if o["side"] == "buy":
            lots[sym].append([q, px, o["t"]])
        elif o["side"] == "sell":
            left, cost, matched, opened = q, 0.0, 0.0, None
            while left > 1e-12 and lots[sym]:
                lot = lots[sym][0]
                take = min(left, lot[0])
                cost += take * lot[1]
                matched += take
                opened = opened or lot[2]
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-12:
                    lots[sym].popleft()
            if matched > 0:
                closed.append({"symbol": sym, "qty": matched, "buy": cost / matched, "sell": px,
                               "pnl": matched * px - cost, "opened": opened, "closed": o["t"]})
    return closed


# ---------- assembly ----------

def _build():
    errors = []

    def safe(name, fn, default):
        try:
            return fn()
        except Exception as e:  # show the problem on the page instead of fake zeros
            errors.append(f"{name}: {str(e)[:200]}")
            return default

    acct = safe("Account", lambda: _get("/v2/account"), None)
    positions = safe("Positions", _positions, None)
    daily = safe("History (all time)", lambda: _history("1A", "1D"), [])
    hourly = safe("History (7 days)", lambda: _history("1W", "1H"), [])
    orders = safe("Trade history", lambda: _merge_by_order(_fills()), None)
    closed = _closed_trades(orders) if orders is not None else None

    s = {}
    if acct:
        eq, last = _f(acct.get("equity")), _f(acct.get("last_equity"))
        s.update(equity=eq, cash=_f(acct.get("cash")), today_pl=eq - last,
                 today_pl_pct=((eq - last) / last * 100) if last else None)
        if daily:
            start_t, start_eq = daily[0]
            s.update(start_equity=start_eq, start_date=start_t, total_pl=eq - start_eq,
                     total_pl_pct=(eq - start_eq) / start_eq * 100 if start_eq else None)
    if positions is not None:
        s.update(invested=sum(p["cost"] for p in positions),
                 market_value=sum(p["value"] for p in positions),
                 unrealized=sum(p["upl"] for p in positions),
                 holdings=len(positions))
    if closed is not None:
        s.update(realized=sum(c["pnl"] for c in closed), closed_count=len(closed),
                 wins=sum(1 for c in closed if c["pnl"] > 0),
                 losses=sum(1 for c in closed if c["pnl"] < 0),
                 winning_gains=sum(c["pnl"] for c in closed if c["pnl"] > 0),
                 losing_losses=sum(c["pnl"] for c in closed if c["pnl"] < 0),
                 trade_count=len(orders))
    if all(k in s for k in ("total_pl", "realized", "unrealized")):
        s["other"] = s["total_pl"] - s["realized"] - s["unrealized"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account_mode": "LIVE" if _is_live() else "PAPER",
        "dashboard_safety": {"us_live_max_usd": _f(os.getenv("US_LIVE_MAX_USD")) if os.getenv("US_LIVE_MAX_USD") else None,
                             "read_only": True},
        "engines": {
            "crypto": {"enabled": _flag("STRATEGY_ENABLED"), "live": _is_live()},
            "us": {"enabled": _flag("US_ENABLED"), "execute": _flag("US_EXECUTE")},
        },
        "summary": s,
        "positions": positions,
        "closed": list(reversed(closed))[:50] if closed is not None else None,
        "activity": list(reversed(orders))[:20] if orders is not None else None,
        "history": {"week": hourly, "all": daily},
        "health": {
            "alpaca_ok": acct is not None,
            "telegram": safe("Telegram", notify.enabled, False),
            "vm": safe("VM metrics", notify.vm_metrics, {}),
            "engine_uptime_s": time.time() - _STARTED,
        },
        "errors": errors,
    }


@router.get("/api/overview")
def overview():
    # Sync on purpose: FastAPI runs it in a worker thread, so slow Alpaca calls never block the server.
    with _LOCK:
        if _CACHE["data"] is not None and time.time() - _CACHE["t"] < CACHE_SECONDS:
            return _CACHE["data"]
        data = _build()
        _CACHE.update(t=time.time(), data=data)
        return data


# ---------- market data for the ticker strip and the candle "tape" ----------

INDEX_PROXIES = [("SPY", "S&P 500"), ("DIA", "Dow"), ("QQQ", "Nasdaq 100"), ("IWM", "Russell 2000")]
TAPE_STOCKS = ["SPY", "QQQ", "IWM"]
TAPE_CRYPTO = ["BTC/USD", "ETH/USD"]
_MCACHE = {}
MARKET_CACHE_SECONDS = 60
BARS_KEPT = 60


def _ts(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _bars(path, symbols, extra):
    start = datetime.fromtimestamp(time.time() - 10 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, token = {}, None
    for _ in range(4):
        params = {"symbols": ",".join(symbols), "timeframe": "1Hour", "start": start, "limit": 10000, **extra}
        if token:
            params["page_token"] = token
        page = _get(path, params, base=DATA_URL)
        for sym, rows in (page.get("bars") or {}).items():
            out.setdefault(sym, []).extend([_ts(b["t"]), b["o"], b["h"], b["l"], b["c"]] for b in rows)
        token = page.get("next_page_token")
        if not token:
            break
    return {k: sorted(v)[-BARS_KEPT:] for k, v in out.items()}


def _build_market(held):
    errors = []
    symbols = [s for s, _ in INDEX_PROXIES] + [h for h in held if h not in dict(INDEX_PROXIES)]
    quotes = []
    try:
        snaps = _get("/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": "iex"}, base=DATA_URL)
        labels = dict(INDEX_PROXIES)
        for sym in symbols:
            snap = snaps.get(sym) or {}
            price = _f((snap.get("latestTrade") or {}).get("p")) or _f((snap.get("dailyBar") or {}).get("c"))
            prev = _f((snap.get("prevDailyBar") or {}).get("c"))
            quotes.append({"symbol": sym, "label": labels.get(sym, "held"), "price": price or None,
                           "prev_close": prev or None,
                           "change_pct": ((price / prev - 1) * 100) if price and prev else None})
    except Exception as e:
        errors.append(f"Quotes: {str(e)[:160]}")
    bars = {}
    try:
        bars.update(_bars("/v2/stocks/bars", TAPE_STOCKS, {"feed": "iex"}))
    except Exception as e:
        errors.append(f"Stock candles: {str(e)[:160]}")
    try:
        bars.update(_bars("/v1beta3/crypto/us/bars", TAPE_CRYPTO, {}))
    except Exception as e:
        errors.append(f"Crypto candles: {str(e)[:160]}")
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "feed": "iex",
            "quotes": quotes, "bars": bars, "errors": errors}


@router.get("/api/market")
def market(held: str = ""):
    """Read-only market data. `held` = comma-separated US tickers to quote besides the index ETFs."""
    extra = sorted({h.strip().upper() for h in held.split(",")
                    if h.strip() and len(h.strip()) <= 6 and h.strip().isalpha()})[:6]
    key = ",".join(extra)
    with _LOCK:
        hit = _MCACHE.get(key)
        if hit and time.time() - hit[0] < MARKET_CACHE_SECONDS:
            return hit[1]
    data = _build_market(extra)
    with _LOCK:
        if len(_MCACHE) > 20:
            _MCACHE.clear()
        _MCACHE[key] = (time.time(), data)
    return data


@router.get("/", response_class=HTMLResponse)
def page():
    return _PAGE.read_text()


@router.get("/dashboard/classic", response_class=HTMLResponse)
def classic_page():
    """Original detailed view; protected by the existing dashboard middleware."""
    return _PAGE.with_name("dashboard_classic.html").read_text()

# Credential setup: protected by the engine's dashboard-session middleware.
# These routes never change LIVE_TRADING, US_LIVE, or execution/approval flags.
import hmac
import re
import tempfile
from fastapi import Request
from fastapi.responses import JSONResponse

_SETTINGS_LOCK = threading.Lock()
_SETTINGS_LAST_ATTEMPT = 0.0
_KEY_NAMES = {
    "paper": ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
    "live": ("APCA_LIVE_API_KEY_ID", "APCA_LIVE_API_SECRET_KEY"),
}


def _settings_reply(data, status=200):
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


def _write_alpaca_keys(mode, key, secret):
    """Atomic owner-only dotenv update; preserve all unrelated configuration."""
    path = _PAGE.with_name(".env")
    if path.is_symlink():
        raise RuntimeError("Unsupported configuration file")
    # Never create a new, partial configuration in the wrong deployment directory.
    original = path.read_text()
    names = _KEY_NAMES[mode]
    replacements = dict(zip(names, (key, secret)))
    lines, seen = [], set()
    for line in original.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        name = match.group(1) if match else None
        if name in replacements:
            if name not in seen:
                lines.append(name + "=" + replacements[name])
                seen.add(name)
        else:
            lines.append(line)
    lines.extend(name + "=" + replacements[name] for name in names if name not in seen)
    fd, tmp = tempfile.mkstemp(prefix=".alpaca-settings-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@router.post("/api/settings/alpaca")
async def save_alpaca_settings(request: Request):
    # Same-origin JSON plus password reauthentication defend this secret-writing route.
    origin = urllib.parse.urlparse(request.headers.get("origin", ""))
    if origin.scheme != "https" or origin.netloc != request.url.netloc:
        return _settings_reply({"error": "Open settings from your secure dashboard URL."}, 403)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return _settings_reply({"error": "JSON request required."}, 415)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            return _settings_reply({"error": "Request too large."}, 413)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return _settings_reply({"error": "Invalid request."}, 400)
    if not isinstance(data, dict):
        return _settings_reply({"error": "Invalid request."}, 400)
    global _SETTINGS_LAST_ATTEMPT
    with _SETTINGS_LOCK:
        if time.monotonic() - _SETTINGS_LAST_ATTEMPT < 5:
            return _settings_reply({"error": "Please wait five seconds before trying again."}, 429)
        _SETTINGS_LAST_ATTEMPT = time.monotonic()
    password, supplied = os.getenv("DASHBOARD_PASSWORD", ""), data.get("password", "")
    if not password or not isinstance(supplied, str) or not hmac.compare_digest(password.encode(), supplied.encode()):
        return _settings_reply({"error": "Dashboard password is incorrect."}, 403)
    mode, key, secret = data.get("mode"), data.get("key"), data.get("secret")
    if mode not in _KEY_NAMES or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,256}", v) for v in (key, secret)):
        return _settings_reply({"error": "Choose an account type and enter both complete Alpaca keys."}, 400)
    # Network and filesystem work must not block the async FastAPI event loop.
    from starlette.concurrency import run_in_threadpool

    def verify_and_save():
        base = "https://paper-api.alpaca.markets" if mode == "paper" else "https://api.alpaca.markets"
        req = urllib.request.Request(base + "/v2/account", headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                account = json.load(response)
            if not isinstance(account, dict) or not account.get("id"):
                return _settings_reply({"error": "Alpaca did not return a valid account. Nothing saved."}, 502)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                return _settings_reply({"error": "Alpaca rejected these keys for the selected account type. Nothing saved."}, 400)
            return _settings_reply({"error": "Alpaca is unavailable. Nothing saved; try again later."}, 502)
        except Exception:
            return _settings_reply({"error": "Could not verify with Alpaca. Nothing saved."}, 502)
        try:
            with _SETTINGS_LOCK:
                _write_alpaca_keys(mode, key, secret)
        except Exception:
            return _settings_reply({"error": "Could not save the server configuration."}, 500)
        return _settings_reply({"ok": True, "restart_required": True, "message": "Keys verified and saved. A service restart is required to load them. Trading modes and approvals were not changed."})

    return await run_in_threadpool(verify_and_save)
