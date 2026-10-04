# AETHER — Strategy & Risk Rules (single source of truth)

Last verified against code: 2026-10-03 (commit after 3322830).
Rule: change this file FIRST, then code, then tests, then commit and deploy.
If this file and the code disagree, trading stops until they agree.

---

## 1. Current mode

| Engine | Mode | Real money? |
|---|---|---|
| Crypto EMA strategy | Shadow recorder (`LIVE_TRADING=false`, broker is paper-only) | No |
| TradingView webhook | Shadow recorder (same crypto broker) | No |
| US ETF momentum | Orders on Alpaca **paper** (`US_EXECUTE=true`, `US_LIVE=false`) | No |

Live Alpaca account `…7342`: active, **unfunded**, shorting disabled.
Crypto live trading is **hard-blocked** in the Risk Controller regardless of settings.

---

## 2. Engines

### 2.1 Crypto trend strategy (`strategy.py`, run by `aether_engine.py`)
- Long-only (Alpaca crypto cannot short). Same code drives the backtester and the live runner.
- Bars: 1 hour (`STRATEGY_BAR_SECONDS=3600`). Engine checks every 60 s (`STRATEGY_INTERVAL_SECONDS`).
- Symbols: BTC/USD, ETH/USD, SOL/USD (`TRACKED_SYMBOLS`, hard-coded in `aether_engine.py`;
  the `.env` `SYMBOLS` value is not read).
- Parameters (`strategy.Params`): fast EMA 50, slow EMA 200, trend EMA 800, RSI 14 (max 70),
  ATR 14 × 3.0, fee 0.25%, slippage 0.05%.
- Entry: EMA cross entry (`ema_cross_entry`) subject to the trend and RSI filters. Exact
  conditions live in `strategy.py`; this file must be updated if they change.
- Exits: see `strategy.py` (ATR-based). Exits are never blocked by any kill switch.
- Sizing: risk 1% of equity per trade (`STRATEGY_RISK_PCT=1`), position capped at 25% of
  equity (`max_notional_pct`), minimum order $10, cooldown 2 h between trades on a symbol
  (`STRATEGY_COOLDOWN_SECONDS=7200`). Exit retries every 300 s.
- Equity used when the broker can't be read: $100,000 (`STRATEGY_SHADOW_EQUITY`).

### 2.2 TradingView webhook (`POST /webhook/tradingview`)
- Auth: `WEBHOOK_SECRET` in header `X-Webhook-Secret` or JSON `secret`. Wrong secret → 401.
- Payload: `symbol`, `condition` (contains BUY or SELL), `price`, `resolution` (required).
- Only the tracked crypto symbols are accepted; anything else is rejected and logged.
- BUY: fixed $100 (`TRADE_NOTIONAL_USD`), refused if above 5% of equity (`MAX_ORDER_PCT`).
- SELL: closes the whole long position; naked shorts are blocked.
- Every attempt (filled, shadow, rejected, error) is written to trade history.

### 2.3 US ETF momentum (`us_strategy.py`, strategy id `us_etf_momentum_v1`)
- Rule fixed in advance, not tuned: monthly, rank SPY, QQQ, IWM, EFA, EEM, IEF, TLT, GLD,
  DBC, VNQ by the average of their 63/126/252-day returns; hold the top 3 at equal weight;
  any pick with a non-positive score is replaced by SHY.
- Signals use closes to the last completed month-end; execution on the next trading day,
  after 15:30 ET.
- Paper allocation $5,000 (`US_ALLOCATION_USD`). Checks every 300 s.
- Failed rebalance: marked incomplete, Telegram report, retry after 900 s.
- Order still unknown after cancel and re-read → `unreconciled` → engine **frozen** until a human
  checks the broker and clears `us.frozen`.
- Holdings are always replaced by the broker's real positions after a rebalance.

---

## 3. Risk limits (where each one lives)

| Limit | Value | Where |
|---|---|---|
| Crypto risk per trade | 1% of equity | `STRATEGY_RISK_PCT` |
| Crypto max position | 25% of equity | `strategy.Params.max_notional_pct` |
| Crypto daily-loss kill switch | 3% below day-start equity → no new entries until UTC midnight (covers the webhook too) | `STRATEGY_MAX_DAILY_LOSS_PCT` + Risk Controller |
| Webhook order size | $100, max 5% of equity | `TRADE_NOTIONAL_USD`, `MAX_ORDER_PCT` |
| US live dollar cap | $100 on strategy equity, buys and account equity | `US_LIVE_MAX_USD` |
| Live order cap | $100 per order | `RISK_MAX_ORDER_USD_LIVE` (default) |
| Live daily buy budget | $100 per UTC day, persisted to disk; **reserve-and-release**: an order errors after the check passed, or fills for less than requested, releases the unspent part back the same day instead of leaving it counted as spent | `RISK_MAX_DAILY_BUY_USD_LIVE` (default) + `risk_controller.release_live_buy()` |
| **US daily-loss kill switch** | 3% below day-start equity → no new US entries | `US_MAX_DAILY_LOSS_PCT` + Risk Controller |
| **US peak-drawdown kill switch** | 27% below peak equity history → no new US entries. This automates the §8 retirement level (-13.5% worst out-of-sample → retire at -27%) instead of relying on a manual monthly check | `US_MAX_DRAWDOWN_PCT` + Risk Controller |
| Approval tolerance | executed plan may differ ≤ 2% from the approved one | `US_APPROVAL_TOLERANCE` |
| Malformed limit config | blocks all entries (fails closed) | `risk_controller.py` |

`RISK_MAX_ORDER_USD_LIVE` and `RISK_MAX_DAILY_BUY_USD_LIVE` are **independent of**
`US_LIVE_MAX_USD`. `plan_orders` splits the cap across up to 3 ETFs, so each buy is roughly
`US_LIVE_MAX_USD / 3`. Keep `RISK_MAX_ORDER_USD_LIVE >= US_LIVE_MAX_USD` (and the daily budget
no smaller), otherwise a plan is refused by pre-flight once the cap is raised above ~$300.
**This is now checked automatically**: on startup, if `US_LIVE=true` and either cap is below
`US_LIVE_MAX_USD`, AETHER logs an error and sends a Telegram alert (it does not refuse to
start, so the paper engine and dashboard keep working either way).

Every US live order also carries a deterministic `client_order_id`
(`us-<rebalance-month>-<symbol>-<side>`). If a submit times out or the connection drops after
Alpaca already accepted the order, the retry looks the order up by that id instead of placing a
second one — the duplicate-order class this closes is the same shape as a crashed TradingView
webhook retry, just on the US side.

Every numeric `.env` value that used to be read with a bare `float()`/`int()` (`US_LIVE_MAX_USD`,
`TRADE_NOTIONAL_USD`, `MAX_ORDER_PCT`, the strategy/US interval and loss-limit settings) now
fails closed to its documented default instead of crashing the whole process at import — a
single malformed value used to take down uvicorn entirely (no engine, no dashboard, no Telegram).

Retired/unused `.env` names (do not rely on them): `MAX_OPEN_POSITIONS`,
`MAX_NOTIONAL_PER_POSITION_PCT`, `RISK_PER_TRADE_PCT`, `MAX_DAILY_LOSS_PCT`, `TRADING_MODE`,
`SYMBOLS`, `TIMEFRAME`, `STATE_DIR`.

---

## 4. The Risk Controller (`risk_controller.py`)

Every broker order passes `risk_controller.check()` inside `alpaca_executor.py`.
For new entries, in order:
1. `~/AETHER_HALT` exists → block everything.
2. Crypto with real money → block.
3. Engine kill switch active or not yet evaluated since start-up → block that engine.
4. Unknown or non-positive size → block.
5. Real money: order > $100 → block.
6. Real money: today's buys + order > $100 → block.
7. US: daily loss ≥ `US_MAX_DAILY_LOSS_PCT` or peak drawdown ≥ `US_MAX_DRAWDOWN_PCT` → block.

**Exits are never blocked — by the halt, the kill switches, or the caps.** A stop must not trap
you in a position.

Both engines are registered in `_KILL` and start as `None` (not yet evaluated), so entries for an
engine are blocked from process start until that engine has reported at least once. Fail closed;
at most one cycle of delay (crypto 60 s, US 300 s).

**A halt blocks entries and rotations, never exits.** A planned rotation (sell some ETFs, buy
others in the same run) is all-or-nothing: the US engine gates the whole rebalance on
`risk_controller.halted()` and calls `risk_controller.preflight()` over every planned order
before submitting any of them. `preflight()` evaluates with `record=False`, so a refused plan
does not consume the real-money daily buy budget and can be retried. Without this, a halt (or a
per-order cap) would let the sells fill and refuse the buys, liquidating the account while the
dashboard reported "HALTED: all new entries blocked".

Every decision is logged to the decision log.

---

## 5. Live gates (US)

Real-money US orders require ALL of:
1. `US_LIVE=true` (and `US_EXECUTE=true`).
2. Live Alpaca client connected (`APCA_LIVE_API_KEY_ID/SECRET`).
3. Dollar cap passes (section 3).
4. A Telegram proposal for **this exact plan, today**, approved with `/approve_<id>` from the
   configured chat. Stale proposals expire; a changed plan invalidates the approval.
5. Time is after 15:30 ET.
6. Risk Controller allows each order.
7. No manual halt: `~/AETHER_HALT` does not exist.
8. The US kill switch is evaluated and clear, and `preflight()` accepts **every** order in the
   plan (all-or-nothing — a plan whose sells would be followed by refused buys is not sent).

**Shutdown no longer severs an in-flight order.** A `systemctl restart` used to set the stop
flags, sleep 2 seconds and exit — if a live order was mid-submission or mid-fill-poll
(`wait_for_fill` can take up to 90s per order), the daemon thread running it was cut off with no
chance to finish, leaving an order live at the broker and never recorded locally. The shutdown
sequence now joins the strategy and US threads (up to 300s) before the process exits, so a cycle
already in progress gets a real chance to complete. `TimeoutStopSec` in `aether.service` is 310s
to match — if you ever lower the join timeout in code, lower this too, and the other way round.

**The US loop now has its own stall check.** Previously only the crypto loop's heartbeat was
monitored (`monitor_cycle`); with crypto ever disabled (`STRATEGY_ENABLED=false`, a plausible
US-only setup) a dead or hung US thread would go unnoticed indefinitely — the dashboard would
just show a stale "last checked" time with nothing actively flagging it. `US_STATUS["last_check_ts"]`
is now checked the same way the crypto loop's `last_cycle_ts` is, and alerts if the US loop hasn't
completed a cycle in `5 * US_CHECK_INTERVAL_SECONDS` (at least 5 minutes).

**Every Alpaca HTTP call now has a timeout.** alpaca-py sets none itself (confirmed: neither
`TradingClient` nor the base `RESTClient` take a timeout parameter) — a single hung request used
to block whichever lock-holding cycle made it indefinitely. A custom `requests` adapter is
mounted onto each client's session with a default of `ALPACA_HTTP_TIMEOUT_SECONDS` (15s).

**systemd now has a real watchdog.** `aether.service` is `Type=notify` with `WatchdogSec=180`:
the app sends `READY=1` once started and `WATCHDOG=1` at the end of every successful monitor
cycle (`monitor_cycle` only reaches that point if VM metrics, service checks and the
now-timeout-bounded broker balance check all returned — so it reliably keeps pinging unless the
process is truly wedged, e.g. a thread stuck holding `STATE_LOCK`). If the ping stops for 180s,
systemd kills and restarts the process. Previously `Restart=always` only covered the process
actually exiting — a hang that never crashed had nothing to recover it.

**Broker call failures are classified, not treated identically.** Every failed Alpaca call used
to collapse into the same generic error, logged and otherwise ignored — nothing backed off on a
rate limit, and nothing ever stopped retrying after repeated failures. `alpaca_executor.py` now
buckets a failure as `timeout` / `connection` / `rate_limited` / `server_error` / `client_error` /
`unknown` (`classify_broker_error`), backs off 2s on a `rate_limited` response, and tracks
`consecutive_errors` on each broker client instance (reset on every success, including a
client_order_id-recovered one). If the US engine hits `BROKER_ERROR_FREEZE_THRESHOLD` (3)
consecutive broker-call failures in a rebalance, it freezes (`us.frozen`) the same way an
unreconciled order already does, instead of silently retrying every cycle against a broker that
is genuinely down.

---

## 6. Emergency procedures

| Situation | Action |
|---|---|
| Stop all new entries now (also blocks a mid-rotation rebalance; exits still work) | `touch ~/AETHER_HALT` (resume: `rm ~/AETHER_HALT`) |
| Stop everything | `sudo systemctl stop aether` |
| US engine frozen | Check orders/positions in Alpaca, fix, then clear `us.frozen` in state |
| Crypto kill switch fired | Wait for UTC midnight; review the decision log first |
| Secret leaked | Rotate immediately (section 7), restart, update TradingView |
| Backup failed alert | `journalctl -u aether-backup`; check SAS expiry and disk space |
| **State corruption alert** (`aether_state.json` *and* `.bak` both unreadable) | AETHER already halted itself (see below); look in `~/*.corrupt.<timestamp>` for the quarantined bad files, check `~/backups` for a usable older copy, restore it to `aether_state.json`, verify it loads, then `rm ~/AETHER_HALT` |

**State file recovery, added 2026-10-04.** If `aether_state.json` is unreadable but `.bak` is
good, `load_state()` recovers from `.bak`, quarantines the corrupt primary
(`aether_state.json.corrupt.<timestamp>`), and immediately repairs the primary from that same
good content. This closes a real bug: previously, recovering from `.bak` without repairing the
primary meant the *next* `save_state()` would copy the still-corrupt primary over the one good
backup, destroying it. If *both* the primary and `.bak` are unreadable, AETHER no longer returns
a silent, empty default state (which would have wiped every open position and reset the
daily-loss baseline with nothing to notice) -- it halts the same way `touch ~/AETHER_HALT`
already does (blocks new entries everywhere, never blocks exits) and sends a critical Telegram
alert. The halt does not clear itself; see the recovery steps above.

**Singleton lock, added 2026-10-04.** Nothing previously stopped two AETHER processes running
at once against the same state files -- a stuck shutdown overlapping a `Restart=always` restart,
or someone running the engine by hand while the service is also up, and both would read/write
`aether_state.json` and could both submit orders. The app now takes an advisory file lock
(`flock` on `~/.aether.lock`, override with `AETHER_LOCK_FILE`) at the very start of startup,
before reading any state or starting any thread. A second instance that can't acquire it logs a
critical error and exits immediately (`raise SystemExit(1)`) rather than running anyway.

**Decision log failures now alert, added 2026-10-04.** `decision_log.py` previously swallowed
every write failure, warning only at exponentially-spaced counts (1, 10, 100, then every 1000)
with nothing else -- trading continued normally while the audit trail silently stopped. It now
alerts on Telegram on the FIRST failure (not after a threshold, since every row lost past that
point is gone for good -- rows are never updated or retried), and again when it recovers.
`decision_log.persistence_degraded()` / `failure_count()` expose the current state for any
caller that wants it. `_code_version()` (the git commit tag stamped on every row) now resolves
once at import instead of lazily on the first log() call during live trading.

---

## 7. Operations

- **Access:** `https://ops.ratelark.app` (Cloudflare Access email code + `DASHBOARD_PASSWORD`).
  Fallback: `ssh -N -L 8000:127.0.0.1:8000 aether` then `http://127.0.0.1:8000`. `/logout` requires
  a valid session and only accepts `POST` (it used to be a public `GET`, so anyone on the internet
  could call it and force every operator session to log out, repeatedly). `/login` locks a source
  out for 5 minutes after 5 wrong passwords in a row (tracked per client IP, alerts on Telegram
  when it trips) -- previously a failed attempt only cost the attacker a 1s sleep, which did
  nothing against many parallel connections.
- **Cloudflare Access JWT validation (`cf_access.py`), added 2026-10-04, not yet activated.**
  Previously the app trusted Cloudflare Access entirely from the network side: any request that
  reached the origin was assumed to have passed the Access policy, with nothing in the app
  itself able to tell. A second hostname or CNAME pointed at the same origin, a too-wide Access
  path pattern, or stale DNS would have exposed `/login` and every `/api/*` route with no
  in-app check to catch it. The app now verifies the `Cf-Access-Jwt-Assertion` header's
  signature, audience and expiry against Cloudflare's own JWKS for any request that doesn't
  arrive via the loopback SSH-tunnel fallback (that path bypasses Access by design, so it's
  exempted by `Host` header instead of by trusting an absent JWT on every path -- trusting an
  absent header everywhere would silently defeat the whole point).
  **To activate it:** set two values in `.env`, from Cloudflare Zero Trust dashboard -> Access ->
  Applications -> the `ops.ratelark.app` application:
  - `CF_ACCESS_TEAM_DOMAIN` -- your team domain, e.g. `yourteam.cloudflareaccess.com`
  - `CF_ACCESS_AUD` -- the Application Audience (AUD) tag shown on that application's page

  Until both are set, `cf_access.verify()` always returns `True` (logged once at startup) and
  nothing changes from today's behavior -- this is additive, not a replacement for the Access
  policy itself, so it was safe to ship before those two values were available.
- **The old `/classic` dashboard (and its `/account`, `/api/stocks`, `/api/history`,
  `/api/portfolio-history`, `/api/snapshot` endpoints) was removed 2026-10-03.** It was the
  pre-rebuild dashboard, superseded by `dashboard_v2.html`, unlinked from any page the dashboard
  actually serves, and had a confirmed stored-XSS hole (broker/webhook text written straight into
  `innerHTML`). If anything ever needs that old UI back, it is in git history before this date --
  it should not be revived as-is given the XSS.
- **Code:** private repo `VenkataRavindra-coder/aether`, deploy key on the VM.
- **Backups:** nightly 03:15 UTC (`aether-backup.timer`): state, decision log, research trials;
  integrity-checked; 14 local copies in `~/backups`; offsite to Azure `aetherbkp/aether-backups`
  (separate resource group, Create-only SAS). `.env` is never backed up.
- **Restore:** download the archive from the Azure portal (the VM token cannot read), extract,
  stop `aether`, copy files back, start `aether`.
- **Rebuild VM:** clone repo, `pip install -r requirements.txt` into `venv`, copy
  `deploy/*.service|timer` to systemd, restore `.env` from the password manager, restore backup.
- **Secrets** (values only in `.env` + password manager): `APCA_API_KEY_ID/SECRET` (paper),
  `APCA_LIVE_API_KEY_ID/SECRET`, `WEBHOOK_SECRET` (rotated 2026-10-03),
  `DASHBOARD_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `BACKUP_SAS_URL` (expires 2027-10-01,
  renew by 2027-09-01).

---

## 8. Research and go-live rules

- Every backtest variant is logged as a trial in `research.sqlite` (`research_trials`),
  including losers. Judge strategies by deflated Sharpe on the true trial count.
- Rules are written down before testing; no tuning on out-of-sample data.
- A new strategy runs in shadow first; forward results decide, not the backtest.
- **Micro-live entry criteria (US only):** all blockers done (done 2026-10-03), this file
  current, account funded with $50–100, `US_LIVE=true`, first rebalance approved by hand.
- **Before scaling above micro-live:** walk-forward validation, Key Vault for secrets,
  a full restore drill, and at least several months of live results matching paper.
- **Strategy retirement rule (2026-10-03):** retire (stop new entries + alert) when EITHER
  (a) profit factor over the last 20 closed trades < 1.0 (checked once 20 exist), or
  (b) drawdown from peak (live or shadow) > 2x the out-of-sample backtest's worst
  (US momentum: -13.5% -> retire at -27%). Exits continue. Re-enable only after a written
  review in this file. No LLM in this decision.
  - (b) is now **automated** for the US engine by the peak-drawdown kill switch
    (`US_MAX_DRAWDOWN_PCT`, default 27 — section 3). It stops new entries and alerts, and never
    clears itself on unknown data while positions are open. (a) remains a manual monthly check.

---

## 9. Change process

1. Edit this file (what changes and why).
2. Change code; run `test_risk_controller.py` and `test_uslive.py` (both must pass).
3. Commit and push; restart `aether`; check `journalctl -u aether` for errors.
4. Never test with real ETF symbols outside market hours (orders queue and fill later).

---

## Risk Governance (live)  — amendment, enforced by safety_guard.py

**Scope.** All rules below use the LIVE capital pool only (marked daily from the live
Alpaca account by `risk_mark.py`). Paper keeps its own limits and never feeds these.

**Halt definition (applies to every breaker and `~/AETHER_HALT`).** A halt blocks NEW
ENTRIES only. Exits (stop-loss sells, closes) are never blocked. Sells that would open a
short are always blocked.

**Mark.** Weekly, monthly and drawdown breakers are judged on one fixed daily mark:
Mon-Fri 15:50 New York time. If the latest mark is more than 4 days old, entries are
blocked (breakers would be blind).

| Breaker | Trigger | Action | Reset |
|---|---|---|---|
| Daily loss | live equity <= prior close - 2% (checked at each entry) | block entries | automatic, next trading day |
| Weekly loss | marked equity <= week anchor - 5% | latch, create `~/AETHER_HALT` | manual review -> `risk_admin.py reset` + `rm ~/AETHER_HALT` |
| Monthly loss | marked equity <= month anchor - 8% | latch, create `~/AETHER_HALT` | manual, as above |
| Drawdown halt | DD from peak >= H, where H = min(10%, 2 x worst out-of-sample DD) | latch, create `~/AETHER_HALT` | manual; reset rebases peak to current equity |
| Drawdown reduce | DD >= 0.75 H | new entry size x 0.5 | automatic when DD recovers |
| Drawdown warn | DD >= 0.50 H | Telegram only | -- |

Warning, reduce and halt are fractions of H, so they always fire in that order.

**Note (2026-10-04): drawdown halt tightened for micro-live.** `GUARD_DD_ABS_CAP` is set to
0.10 (10%) here as the cap, but the operator has chosen a tighter working ceiling while the
live account is funded at the $100 micro-live level -- see `US_MAX_DRAWDOWN_PCT=15` (a
separate, pre-existing kill switch on the engine's own paper/live equity history,
`_us_evaluate_kill_switch` in `aether_engine.py`). These two breakers are independent and
both gate: whichever is stricter at any moment wins. They do not share a ledger or a data
source -- this one marks live equity once daily via `risk_mark.py`; the engine's own kill
switch samples once daily into `equity_history`. Reconciling them into one system is a
later improvement, not required for this to be safe (both fail toward blocking entries,
never toward allowing something the other would have blocked).

**Precedence.** Retirement rule (PF of last 20 closed trades < 1.0, or DD > 2x worst
out-of-sample backtest DD) stays active. When rules overlap, the most conservative wins.

**Sizing.** Risk per trade <= 1% of live equity. If the computed notional (after any
reduction) is below the broker minimum ($1 fractional), the trade is skipped -- never
rounded up.

**Cash flows.** Fund the micro-live account once. Any deposit/withdrawal must be recorded
(`risk_admin.py cashflow +-X`) the same day so it is not counted as P&L or drawdown.

**Monitoring.** An external watchdog (separate systemd timer, every 5 min) alerts when
aether.service is down, the engine heartbeat is >15 min old, or the risk mark is stale, and
lists any open live positions (their stops are engine-managed, so they are unprotected
while the engine is down). Optional off-VM dead-man ping via `HEALTHCHECK_URL`.
