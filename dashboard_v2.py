"""AETHER dashboard v2.

Adds two routes to the existing FastAPI app:
  GET /              the AETHER board (board.html, 2026-10-21): one screen, every panel live
  GET /console       the detailed console (console.html, re-read on every request)
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

_PAGE = Path(__file__).with_name("console.html")
_BOARD = Path(__file__).with_name("board.html")
_REPO_DIR = Path(__file__).resolve().parent   # engine state file and backup.sh output live here
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
    """The headline account follows the only engine that can spend real money: US_LIVE.
    (LIVE_TRADING is the crypto shadow flag; crypto real money is blocked by the risk controller.)"""
    return _flag("US_LIVE")


def _headline_mode():
    return "live" if _is_live() else "paper"


def _base_url():
    return "https://api.alpaca.markets" if _is_live() else "https://paper-api.alpaca.markets"


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _creds(mode):
    """API key/secret for an explicit account, bypassing the LIVE_TRADING-based default.
    mode=None is not handled here; callers needing the legacy single-account behavior
    keep using _env(...) / _base_url() directly, unchanged."""
    if mode == "live":
        return _env("APCA_LIVE_API_KEY_ID"), _env("APCA_LIVE_API_SECRET_KEY")
    if mode == "paper":
        return (_env("APCA_API_KEY_ID", "ALPACA_API_KEY", "ALPACA_KEY_ID"),
                _env("APCA_API_SECRET_KEY", "ALPACA_SECRET_KEY", "ALPACA_API_SECRET"))
    return "", ""


def _account_summary(mode):
    """Cheap read-only snapshot (just GET /v2/account, no positions/history/fills) so showing
    both paper and live side by side on the dashboard stays fast even though it doubles the
    number of Alpaca calls the page makes. Never writes, never touches LIVE_TRADING/US_LIVE."""
    key, sec = _creds(mode)
    if not key or not sec:
        return {"configured": False}
    base = "https://api.alpaca.markets" if mode == "live" else "https://paper-api.alpaca.markets"
    req = urllib.request.Request(base + "/v2/account",
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        a = json.loads(r.read().decode())
    eq, last = _f(a.get("equity")), _f(a.get("last_equity"))
    return {"configured": True, "equity": eq, "cash": _f(a.get("cash")),
            "today_pl": (eq - last) if last else None,
            "today_pl_pct": ((eq - last) / last * 100) if last else None}


DATA_URL = "https://data.alpaca.markets"


def _get(path, params=None, base=None):
    # Keys always match the account the base URL points at (live keys for the live API).
    key, sec = _creds(_headline_mode())
    if not key or not sec:
        raise RuntimeError(f"Alpaca {_headline_mode()} API keys are not in the environment")
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
        "accounts": {
            "paper": safe("Paper account", lambda: _account_summary("paper"), {"configured": False}),
            "live": safe("Live account", lambda: _account_summary("live"), {"configured": False}),
        },
        "engines": {
            "crypto": {"enabled": _flag("STRATEGY_ENABLED"), "live": _flag("LIVE_TRADING")},
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
            "telegram_dropped": safe("Telegram drops", notify.dropped_count, None),
            "vm": safe("VM metrics", notify.vm_metrics, {}),
            "engine_uptime_s": time.time() - _STARTED,
        },
        "errors": errors,
    }


_BUILD_LOCK = threading.Lock()


def _fresh_cache():
    with _LOCK:
        if _CACHE["data"] is not None and time.time() - _CACHE["t"] < CACHE_SECONDS:
            return _CACHE["data"]
    return None


@router.get("/api/overview")
def overview():
    # Sync on purpose: FastAPI runs it in a worker thread, so slow Alpaca calls never block the server.
    # _LOCK only guards the cache dict and is never held across network calls. One request at a
    # time rebuilds (_BUILD_LOCK); while it does, others get the previous result marked stale
    # instead of queueing behind up to ~50 paged Alpaca calls.
    data = _fresh_cache()
    if data is not None:
        return data
    if not _BUILD_LOCK.acquire(blocking=False):
        with _LOCK:
            last = _CACHE["data"]
        if last is not None:
            return {**last, "stale": True}
        _BUILD_LOCK.acquire()   # very first load: nothing to serve yet, wait for the builder
    try:
        data = _fresh_cache()  # another request may have finished the rebuild meanwhile
        if data is not None:
            return data
        data = _build()
        with _LOCK:
            _CACHE.update(t=time.time(), data=data)
        return data
    finally:
        _BUILD_LOCK.release()


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
def board():
    """The board (2026-10-21): one screen of numbered panels fed by the same read-only APIs as the
    console. It has no write actions, so no CSRF token; each panel links to its /console section."""
    return HTMLResponse(_BOARD.read_text(), headers={"Cache-Control": "no-store"})


@router.get("/console", response_class=HTMLResponse)
def page():
    """The detailed console (2026-10-18): every former dashboard (v2, classic, /us, /map) as one page
    with a left menu. The CSRF token is the one /api/us/pause checks (the only write on the page)."""
    import us_dashboard
    return HTMLResponse(_PAGE.read_text().replace("__CSRF__", us_dashboard.CSRF_TOKEN),
                        headers={"Cache-Control": "no-store"})


@router.get("/dashboard/classic")
def classic_page():
    """Retired 2026-10-18: merged into the console."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/console#holdings", status_code=307)

# Alpaca keys are never written from the browser. The former POST /api/settings/alpaca route
# (password re-entry, then rewrite .env) was removed 2026-10-17: credentials reach the server over
# SSH only, so a dashboard or Access compromise can never plant or swap live-trading keys.


@router.get("/api/safety")
def safety():
    """Read-only safety snapshot: live mode, manual halt, live buy budget, last backup."""
    import glob, json, time
    from pathlib import Path
    home = Path.home()
    import safety_guard
    halt = safety_guard.halt_file().exists()
    spent = 0.0
    try:
        import risk_controller
        s = json.loads(risk_controller._state_path("us").read_text())
        if s.get("day") == time.strftime("%Y-%m-%d", time.gmtime()):
            spent = float(s.get("usd", 0.0))
    except FileNotFoundError:
        pass
    except Exception:
        spent = None
    # backup.sh writes to <repo>/backups (its HOME_DIR), not ~/backups.
    archives = sorted(glob.glob(str(_REPO_DIR / "backups" / "aether-*.tar.gz")))
    last = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(archives[-1]))) if archives else None
    return {"us_live": _flag("US_LIVE"), "halt": halt,
            "live_budget_usd": _f(os.getenv("RISK_MAX_DAILY_BUY_USD_LIVE") or 100),
            "live_spent_today_usd": spent, "last_backup_utc": last,
            **dict(zip(("proposal", "last_rebalance_month"), _us_proposal_info()))}


def _us_proposal_info():
    """(proposal {id,status,date} or None, last_rebalance_month) for the active US mode."""
    import json
    from pathlib import Path
    try:
        # Same file the engine writes: aether_engine.STATE_FILE is <repo>/aether_state.json.
        st = json.loads((_REPO_DIR / "aether_state.json").read_text())
    except Exception:
        return None, None
    u = st.get("us_live" if _flag("US_LIVE") else "us_paper") or {}
    p = u.get("live_proposal") or None
    prop = {k: p.get(k) for k in ("id", "status", "date")} if p else None
    return prop, u.get("last_rebalance_month")
