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
| Canary order cap (phase 1) | $15 per order, notional; never looser than `RISK_MAX_ORDER_USD_LIVE` | `CANARY_MAX_ORDER_USD` + Risk Controller (key `canary`) |
| Canary daily buy budget (phase 1) | $30 per UTC day, own state file; never consumes the engine's live budget | `CANARY_MAX_DAILY_BUY_USD`, `AETHER_CANARY_RISK_STATE` |
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

**The `canary` key (micro-live phase 1, section 10).** The only key besides `us` allowed to
place real-money entries. It has no daily-loss kill switch (it never holds more than one tiny
position); rules 5 and 6 use the canary's own caps instead (`CANARY_MAX_ORDER_USD`, default $15,
capped by `RISK_MAX_ORDER_USD_LIVE`; `CANARY_MAX_DAILY_BUY_USD`, default $30, in its own state
file). A canary buy can therefore never use up the budget a live rebalance needs. Rule 1 (halt)
and the safety_guard live gate apply unchanged; canary exits pass the halt like every exit.

**One halt file (2026-10-14).** Every component resolves the halt file through
`safety_guard.halt_file()`: `AETHER_HALT_FILE` if set, else `AETHER_HOME/AETHER_HALT`, else
`~/AETHER_HALT`. Before this, breaker latches were written to `AETHER_HOME/AETHER_HALT` while
`risk_controller` and the main dashboard read `AETHER_HALT_FILE`. `canary.py preflight` fails if the
paths diverge or a stale file sits at the old breaker path.

**A latched live breaker refuses real-money entries in `risk_controller` too (2026-10-14).** Not
only at submit time in `safety_guard`: because `preflight()` runs the same rule, a latch refuses
a whole live rotation, even if the halt file was removed without `risk_admin.py reset`. An
unreadable ledger counts as latched. Exits and paper are unaffected.

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

**Live orders are marketable limit orders (2026-10-17 audit).** Real-money US orders no longer go
out as market orders. `submit_market_order` on the LIVE client reads the latest quote and sends a
DAY limit: BUY at `ask × (1 + LIVE_LIMIT_BAND)`, SELL at `bid × (1 − LIVE_LIMIT_BAND)` (default
0.2%). It refuses the order (`guard:no_quote` / `bad_quote` / `wide_spread`) when there is no
quote, the quote is zero or crossed, or the bid/ask spread exceeds `LIVE_MAX_SPREAD` (default
0.5% of mid). BUY quantity is `notional / limit` rounded down, so a fill can never exceed the
reserved notional. Paper keeps market orders so the running soak's execution model is unchanged.
All share quantities are rounded DOWN to 9 decimals (`us_strategy.qty9`): Alpaca rejects more,
and a computed trim (e.g. `0.041152263040999996`) used to be sent unrounded.

**"Maybe placed" keeps the budget reserved.** A submit that fails with a timeout, dropped
connection or 5xx, where the client_order_id lookup also fails, returns `outcome_unknown`. Its
real-money daily budget is NOT released (the money may be spent); only a definite refusal (4xx)
returns it. A live SELL whose position cannot be read is refused as `guard:position_unknown`
rather than mislabelled as a naked short. Order-failure alerts and the decision log now carry the
broker's reason (`detail`), and the alert de-dup key names which orders failed, so a different
failure inside the hour is not silenced.

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
- **One console (2026-10-18).** `https://ops.ratelark.app/` is the only dashboard: a left menu with
  Overview, Holdings & trades, US strategy, Orders, Market, Crypto (shadow), Safety, System map and
  Health & jobs (`console.html`). It replaces `dashboard_v2.html`, `dashboard_classic.html` and the
  `/us` page; `/us` and `/dashboard/classic` redirect into it, and `/map` is shown inside it. Its only
  write is "Pause entries" (two clicks, CSRF-checked, creates the halt file); resuming stays SSH-only.
- **The old `/classic` dashboard (and its `/account`, `/api/stocks`, `/api/history`,
  `/api/portfolio-history`, `/api/snapshot` endpoints) was removed 2026-10-03.** It was the
  pre-rebuild dashboard, superseded by `dashboard_v2.html`, unlinked from any page the dashboard
  actually serves, and had a confirmed stored-XSS hole (broker/webhook text written straight into
  `innerHTML`). If anything ever needs that old UI back, it is in git history before this date --
  it should not be revived as-is given the XSS.
- **Code:** private repo `VenkataRavindra-coder/aether`, deploy key on the VM.
- **Backups:** nightly 03:15 UTC (`aether-backup.timer`): state, decision log, research trials;
  integrity-checked; 14 local copies in `~/backups`; offsite to a Hetzner Storage Box
  (`BACKUP_STORAGEBOX`, rsync over SSH port 23, dedicated key, folder `aether-backups/`). The
  Azure target (`aetherbkp`) was lost with the Azure resources on 2026-10-07/08; `backup.sh`
  still supports `BACKUP_SAS_URL` but it is no longer set. `.env` is never backed up.
- **Restore:** fetch the archive from the Storage Box (sftp/rsync with the same key, or the
  Hetzner console), extract, stop the engine service (`ratelark` on Hetzner), copy files back,
  start it.
- **Rebuild VM:** clone repo, `pip install -r requirements.txt` into `venv`, copy
  `deploy/*.service|timer` to systemd, restore `.env` from the password manager, restore backup.
- **Secrets** (values only in `.env` / `.env.live` + password manager): `APCA_API_KEY_ID/SECRET`
  (paper), `APCA_LIVE_API_KEY_ID/SECRET` (in `.env.live`, mode 600: loaded only by
  `aether.service`, `canary.py`, `live_check.py`, `risk_mark.py` and `risk_admin.py`, so the
  backup/daily/health/stress/watchdog timers never hold real-money keys; keys still in `.env` keep
  working), `WEBHOOK_SECRET` (rotated 2026-10-03),
  `DASHBOARD_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `BACKUP_SAS_URL` (expires 2027-10-01,
  renew by 2027-09-01).

---

## 8. Research and go-live rules

- Every backtest variant is logged as a trial in `research.sqlite` (`research_trials`),
  including losers. Judge strategies by deflated Sharpe on the true trial count.
- Rules are written down before testing; no tuning on out-of-sample data.
- A new strategy runs in shadow first; forward results decide, not the backtest.
- **Micro-live entry criteria (US only), in two phases (2026-10-14):**
  - **Phase 1, canary (execution only):** all blockers done (done 2026-10-03), this file
    current, account funded with $50–100, `US_LIVE=false`, orders only from `canary.py`
    (section 10). No strategy signal reaches the live account. A clean phase 1 validates
    execution only.
  - **Phase 2, strategy signals at micro size:** phase 1 conclusive, `canary.py assert-flat`
    passes, **and** the strategy has passed its validation gate (DSR >= 0.95, PBO <= 0.05)
    and the Monte Carlo drawdown is within the 27% limit. Only then `US_LIVE=true`, first
    rebalance approved by hand. Phase 1's result has no bearing on this gate.
- **Strategy verdict record: `us_etf_momentum_v1` (S2, recorded 2026-10-14).**
  Source: `aether-research/us_momentum/results/strategy_truth_yf_20261007_074638.json`
  (committed), produced by `strategy_truth.py run` as pre-registered in
  `docs/PREREG_us_momentum.md`.
  - Run: 2026-10-07 07:46 UTC, 47 minutes after tag `prereg-us-momentum` (06:59 UTC).
  - Code fingerprint: `c879622ee75bc0aa97b9422649cda1fdc8f3a31b21a3ece9285e5ad78af69a7d`
  - Data fingerprint: `e9524a2a60e1c609af72c991d1be8cc8d42f36d95bcafcf1b1b81dd1971ed9b6`
    (long Yahoo history; S14 PASS: agrees with the Alpaca sample, worst median difference
    1.5 bp, signals agree 97.4%).
  - Window: 2007-03-01 to 2026-10-06 (full pre-registered sample, not a holdout).
  - Sharpe 0.779; CAGR 10.8%; observed max drawdown -20.7%; Calmar 0.521.
  - Deflated Sharpe: **0.802 -> FAIL** (gate 0.95; N = 28; Sharpe 0.96 would be needed).
  - PBO (S3): **not run** (stage 2). Deferred, not passed: the gate is DSR >= 0.95 **and**
    PBO <= 0.05, and S2 already fails it.
  - Monte Carlo max drawdown, 95th percentile (S11): **30.7% -> FAIL** (limit 27%).
  - N: 28 (4 logged + 24 backfilled).
  - **Verdict: NO-GO.** Fails the deflated-Sharpe gate and the drawdown limit. Phase 2 (strategy
    signals on the live account) does not start for this rule. Paper continues unchanged
    (prereg section 9).
  - Benchmark, part of the result (S17): the Sharpe equals a plain 60/40 portfolio's over the
    same window (0.779 vs 0.779; SPY 0.639). The only advantage is Calmar (0.52 vs 0.27 for
    60/40): the same return per unit of risk with smaller drawdowns. That is a risk-management
    property, not evidence of a momentum edge.
  - **Passive benchmark (added 2026-10-14; a benchmark, not a trial: N stays 28).** Does the
    momentum *timing* beat simply holding the same 11 ETFs? Passive rule: equal weight (1/11,
    SHY included), same monthly schedule, run through the same simulator (same open execution,
    2 bp/side, same data `e9524a2a…`, identical window). Source:
    `results/benchmark_equal_weight_yf_20261014_105520.json`, `benchmark_equal_weight.py`. The
    strategy side reproduces the S2 numbers exactly.

    | | Timing (S2) | Passive equal-weight |
    |---|---|---|
    | CAGR | 10.8% | 7.3% |
    | Sharpe | 0.78 | 0.63 |
    | Max drawdown | -20.7% | -36.9% |
    | Calmar | 0.52 | 0.20 |
    | MC drawdown, 95th pct | 30.7% | 33.9% |
    | Bootstrap Sharpe, 5th pct | 0.46 | 0.28 |
    | Turnover per year | 6.0x | 0.4x |

    Timing value: +3.5 pp a year, information ratio 0.28 over 19.6 years, **not statistically
    significant.** Method: the series tested is the daily return difference (timing minus
    passive), 4,931 days. t = 1.24 assuming independent days; Newey-West (HAC) t = 1.54 with 21
    lags and 1.66 with 63 lags; a 21-day block bootstrap (5,000 draws, seed 20261007) gives
    P(mean <= 0) = 0.07, one-sided. The robust figures are a little higher (daily active returns
    are negatively autocorrelated) but all remain below conventional significance, and this
    comparison was chosen after S2 failed. It is concentrated: 2008 alone contributed about 24 pp; excluding
    2008 the information ratio is 0.22 (t = 0.97). Timing beat passive in 13 of 20 years.
  - **Reading.** On point estimates the timing does add value over its own universe, mostly by
    avoiding the 2008 drawdown, which is why its Calmar is better. The margin cannot be told apart
    from luck. This strengthens the interpretation of S2 (the rule is not just its ETFs) but does
    not change the verdict: NO-GO stands, and nothing here supports a variant of this rule as
    trial 29.
  - Going live with this rule anyway would be a written RISK ACCEPTANCE in this file (amount,
    reason, sunset date, stop rule), never a pass. Any changed rule (e.g. a regime filter) is a
    new rule with its own pre-registration, and every variant adds to N.
- **Decision 2026-10-14: Option 1 (continue observation), trial 29 deferred.** Recorded before
  any soak or holdout result exists. Supplements the S2 record above; changes no code, no
  threshold and no existing result.
  - **Selected:** paper observation continues; the $15 VTI execution canary (section 10) runs
    after the wire credits and `canary.py preflight` passes. A clean canary validates execution
    only and never authorizes strategy-driven live orders.
  - **Unchanged:** S2 verdict NO-GO; N = 28; trial 29 deferred (it needs a genuinely different,
    independently motivated hypothesis and its own pre-registration). Autonomous live strategy
    orders stay disabled (`US_LIVE=false`).
  - **Strategy freeze:** no rule or parameter changes to `us_strategy.py` during the soak. Per
    test plan section 6, any engine code change (as defined by check A4) restarts the soak clock.
  - **Soak start (not backdated):** the first server-verified timestamp at which every soak
    prerequisite is green, including daily automation A1-A6 and the A5 stress record, at a
    deployed commit with no later engine code change. **Not yet established**; to be filled in
    here from server evidence: `SOAK_START = (pending)`. Earlier paper activity (resumed
    2026-10-13 after stale positions were closed) is kept as records but is not qualifying soak
    evidence. Engine code was changed and redeployed on 2026-10-14 (PR #1, PR #2, dashboard
    extras), so no soak clock can predate that deployment.
  - **Soak completion (all required, test plan section 6):** at least 30 calendar days from
    SOAK_START; at least two completed month-end rebalances; at least 10 qualifying paper fills
    (if fewer, extend until 10); plus the section 6 pass criteria and green daily automation
    every day.
    - *Qualifying paper fill:* an order placed by the US engine on the strategy paper account
      (`client_order_id` starting `us-`) that reached a final broker state with filled quantity
      above zero, booked by the engine and reconciled. Excludes the stress account, canary orders
      (live account), manual trades and the 2026-10-13 closing of stale positions.
    - *Completed rebalance:* a decision-log `rebalance` event with `decision=complete` for a
      month key after SOAK_START (one per month; an `incomplete` run counts only if a retry in
      the same month completes it).
  - **Earliest evaluation:** early December 2026 (the two-rebalance condition binds), and only if
    every condition is met by then. Otherwise the soak extends; January is a possible checkpoint,
    not an automatic opening date. The decision point is the conditions being met, not a date.
  - **S4 holdout:** all market data after 2026-10-07 (prereg section 6), the same calendar period
    as the soak, so it is a forward period, not a dataset set aside in advance. Evaluated
    **once**, after soak completion, with the frozen rule and the frozen criterion (test plan S4:
    net-positive AND holdout Sharpe >= 0.5 x in-sample Sharpe), recorded in `docs/holdout.lock`.
    It is a **forward consistency check**, not statistical proof of an edge: a few monthly
    rebalances cannot establish significance. No interim peeking, no threshold changes, no
    window changes. Passing S4 does not by itself reverse the NO-GO; every gate still applies.
  - **S4 outcomes, fixed now (2026-10-14), before any holdout data is examined:**
    - *Measure:* the frozen rule simulated by `strategy_truth.py` over all data after 2026-10-07
      up to the soak's completion date, same costs and execution model as S2.
    - *Pass* = net return > 0 **and** annualised Sharpe >= 0.5 x 0.779 = **0.39** (test plan S4;
      in-sample Sharpe frozen at the S2 value). Anything else is a fail. No tolerance band, no
      rounding, no "broadly consistent".
    - *If pass:* recorded as "not falsified forward". The NO-GO stands (S2 failed); live strategy
      trading still requires every gate or a written risk acceptance. Nothing else follows.
    - *If fail:* `us_etf_momentum_v1` is retired from any live consideration permanently. Paper
      may continue only as operational testing of the engine.
    - *Power caveat:* two or three monthly rebalances make this Sharpe extremely noisy; either
      outcome is weak evidence. That is why S4 can only falsify, never establish, an edge.
    - *Paper-vs-simulation (test plan section 6, secondary, reported only):* realised paper net
      return inside the simulation's 90% band. Reported, never used to override S4.
    - *Endpoint:* S4 is evaluated once and is final. It is not a trigger for further tests,
      re-runs or a longer window.
  - **Canary vs the soak clock (test plan check A4 decides, unchanged):** *running* the canary
    changes no code and never restarts the clock (it trades a different account, and canary
    fills never count as soak fills). *Changing* code does: A4 counts every `.py` except tests,
    the stress script, research code, patches and the daily runner, so an edit to `canary.py`,
    `alpaca_executor.py`, `risk_controller.py` or `safety_guard.py` restarts the soak. That is
    intended for the shared order-path files; for `canary.py` it is conservative, and is
    accepted rather than narrowed now (narrowing A4 would be a test-plan change under its
    section 12). In practice the canary is expected to run before SOAK_START.
  - **What can extend the soak:** only the section 6 conditions not yet being met (days,
    completed rebalances, fewer than 10 qualifying fills, a non-green day, or an engine code
    change restarting the clock). Never the results.
  - **Registration 2026-10-19: three daily-frequency candidates, research only.** S-A (daily
    200-day trend filter on six ETFs), S-B (SPY RSI(2) mean reversion) and S-C (TLT turn-of-month,
    09:45 ET last trading day to 09:45 ET on the 4th trading day of the next month) are fixed in
    `docs/PREREG_daily_v1.md`, tag `prereg-daily-v1`, before any result exists. This is the
    "genuinely distinct, pre-registered hypothesis" that unblocks trial 29, and it counts three
    trials: N is 31 or more for their DSR (more if `daily_edges_backtest.py` logged trials outside
    the 28). The S2 record is unchanged. Changes no code, no threshold, no live setting; the
    canary and `US_LIVE=false` are untouched, and no result authorizes live orders. The holdout
    stays closed.
    - **Amendment 1, 2026-10-19 (before any result):** `docs/PREREG_daily_v1_amendment1.md`, tag
      `prereg-daily-v1.1`. The session open is not the 09:45 ET price the rules specify, so a fifth
      criterion (execution-price robustness on real 09:45 fills over the intraday window) now gates
      every candidate; the open-proxy run stays for the long history but can never pass alone. It
      also fixes a separate holdout execution path and a manual trial inventory. The runner's `run`
      stays locked until the five steps in its section 5 are done.
    - **Amendment 2, 2026-10-19 (before any result):** `docs/PREREG_daily_v1_amendment2.md`, tag
      `prereg-daily-v1.2`. The server's trial database holds 0 logged trials and one restored row of 28, so the
      trial-Sharpe variance for the deflated Sharpe is now `max(0.000324276, variance of logged trials incl. the
      candidates)`: the new trials can raise the bar, never lower it below the stage-1 record.
  - **Incidents on record (no trades resulted from either):**
    - 2026-10-13 18:42 UTC: the `/us` dashboard "Pause new trades" wrote
      `~/ratelark/AETHER_HALT` (old breaker path). The engine read `~/AETHER_HALT` and never saw
      it, so the pause had no effect while the dashboard showed paused. No orders were placed in
      that window. Removed 2026-10-14; root cause fixed by the one-resolver change (PR #1).
    - 2026-10-14, about 06:37-06:46 UTC: a server test run created the real `~/AETHER_HALT`
      (content `x`); paper entries were blocked for about 10 minutes. No rebalance was due and
      no orders were affected. Removed by hand; test isolation fixed (PR #2).
    - 2026-10-14 ~08:00 UTC to 2026-10-15 (until the fix is deployed): the new `aether-health`
      check (PR #9) treated the login middleware's 401 as "dead" and restarted
      `ratelark.service` about every 90 s; the watchdog alerted on the gaps. No rebalance was
      due, so no orders were affected. Fixed in PR #11 (any HTTP reply counts as alive,
      regression test added). Before SOAK_START, so no soak clock was running.
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

## 10. Micro-live phase 1 (canary)

**Purpose.** Validate execution plumbing against the live Alpaca account with real money at
minimum size. Not a strategy test: no strategy signal drives any order in this phase.

**How it is built (2026-10-14).**
- `canary.py` is a standalone script with its own LIVE client. The engine is not involved and
  stays `US_LIVE=false`, so it never reads or trades the live account in this phase. There is
  no live override inside the engine; `tests/test_canary.py` asserts the engine source never
  references the canary path.
- Same plumbing as the engine's live path: orders go through `alpaca_executor`
  (`_canary_submit_limit_order`, called only by `canary.py`), `risk_controller` under the
  `canary` key (section 4), and the `safety_guard` live gate and ledger. `canary.py` loads the
  repo `.env` before importing them, so it sees the same halt file, risk state and ledger as
  the engine.
- Orders are fractional DAY limit orders on **VTI**, which is outside the strategy universe.
  SPY is in the universe: a leftover canary on it would block the first live rebalance.
- Every canary `client_order_id` starts with `canary-` (+ UUID, well under Alpaca's 128-char
  limit). Nothing is stored locally: each run re-reads the broker and classifies from
  `client_order_id` alone, so every invocation is a cold start. The position-to-order mapping
  is canary-only (one-in-one-out means one originating order); do not reuse it for strategy
  positions. A canary position with any non-canary fill is "unknown" and `canary.py` refuses to
  touch it.
- One in, one out: `buy` refuses while a canary position or canary open order exists.
- Canaries run only while the live account holds no strategy positions and `US_LIVE=false`;
  `canary.py` refuses otherwise. In phase 2, canaries stop.

**Prerequisites, in this order (checked by `python3 canary.py preflight`).**
1. The wire has credited (pending is not credited).
2. Initialise the live ledger: `python3 risk_admin.py mark` (reads live equity). The first mark
   *is* the baseline, so do **not** also run `risk_admin.py cashflow +100` for this first
   deposit; that would count it twice and the breakers would see a 50% drawdown. `cashflow` is
   only for deposits/withdrawals after the ledger exists. Preflight fails if ledger and broker
   equity disagree.
3. Account type confirmed in writing by Alpaca (2026-10-19: Alpaca supports **margin accounts
   only**; cash accounts are on their roadmap with no date). A `multiplier` of 1 is an unleveraged
   margin account under $2,000 equity, not a cash account, so the PDT rule applies. Preflight
   prints the broker's `multiplier`.
4. `python3 canary.py preflight` reports PASSED.

**Day-trade constraint.** Every Alpaca account is margin, so under $25k a 4th same-day round
trip in 5 business days can flag the account (multiplier 1 included; fixed 2026-10-19, the
check had skipped multiplier 1). `canary.py buy` refuses when the broker's day-trade count
is 3 or more. Prefer holding canary positions overnight (sell next session).

**Schedule.** Run in order; do not advance until the step passes.
1. **Baseline round trip (fill quality).** `buy --usd 8 --limit <at/near ask>`, then `sell
   --limit <at/near bid>` (next session if margin). The output records submit time, ids and the
   quote at submit. Pass: both legs fill; `status` matches the broker; no phantom positions.
2. **Open order across a cold start.** `buy` with a limit far below market; run `status` in a
   new process; restart `ratelark.service` and confirm it does not touch the live account
   (`US_LIVE=false`); `status` again. Pass: every canary order classified from
   `client_order_id` alone, nothing in strategy state; `cancel` succeeds.
3. **Exit across a cold start.** Open a tiny position; in a new process `status` must classify
   it as canary from broker data alone; then `sell`. Pass: classification correct; position
   flat; broker and `status` agree.
4. **Halt under a live position.** Open a tiny position; `touch ~/AETHER_HALT`; `buy` must be
   refused (`risk:halted`); `sell` must go through (halt blocks entries, never exits — section
   4); alert fired; `rm ~/AETHER_HALT`. Pass: no entries after halt; exit filled; state
   reconciled.
5. **Cancel/replace.** `buy` far from market, `cancel`, `buy` again at a new limit. Pass: acks
   for each; one final resting order; no duplicates.
6. **Fractional limit orders, live.** Record whether the live account accepts fractional DAY
   limit orders and any constraints. "Not supported" is a pass if recorded; the canary then
   needs whole-share orders and this section is amended before continuing.

**End of every session:** `python3 canary.py assert-flat` must print FLAT (exit 0). A session
that cannot get flat is an incident: halt, cancel, sell, then investigate.

**Conclusive when:** every step 1–6 passed at least once; steps 1 and 3 repeated to 10
completed round trips in total; zero reconciliation mismatches, zero duplicate orders, zero
post-halt entries; final `assert-flat` passes. Partial fills and trading halts stay in the
paper stress pack.

**Execution measurement (pre-registered 2026-10-17, before any live canary order).** Every
filled canary leg is measured from Alpaca's own records, not from our logs or the dashboard:
the order object (`submitted_at`, `filled_at`, `filled_avg_price`) and the raw FILL activities
(`/v2/account/activities/FILL`), against the bid/ask that `canary.py` recorded at submit.
`python3 canary.py report [--since YYYY-MM-DD]` computes, per leg: implementation shortfall in
bp vs the mid at submit (signed so + is cost), spread at submit, and time to fill; it checks the
FILL activities against the order (qty and VWAP); pairs buys and sells into round trips (one in,
one out); and reconciles each session's cash: the account cash at `assert-flat` minus the cash
before the session's first canary order must equal that session's canary fill cash flows. It
writes `tests/evidence/canary/report-*.json` (backed up nightly) and prints PASS or FAIL against
the thresholds below **only**. The thresholds are constants in `canary.py` (`PREREG`), not
`.env` values; a test fails if the two copies differ. Changing them takes a commit made before
the session it applies to, and the reason goes in this section.

```prereg
max_fill_seconds: 900
max_abs_shortfall_bp: 10.0
max_spread_bp: 10.0
max_cash_residual_usd: 0.05
window_et: 10:00-15:45
target_round_trips: 10
```

- A leg FAILS on any of: time to fill over 900 s; |shortfall| over 10 bp; spread at submit over
  10 bp; no usable quote recorded at submit (a sell is never blocked
  for lacking one, but it then cannot pass); FILL activities that disagree with the order.
- A session FAILS if its cash residual exceeds $0.05 (room for regulatory sell fees only; a
  deposit, fee or stray trade that day surfaces here) or if start/end cash was not recorded.
  `buy` records cash before; `sell` records cash before; `assert-flat` records cash at the end.
- Protocol FAILS: two canary buys without a sell between them, a sell without an open buy, or
  not flat at report time.
- `buy` refuses outside 10:00–15:45 ET, on weekends, when Alpaca's clock says the market is
  closed, or when the clock cannot be read. `sell` is an exit and is never time-gated.
- Why the spread line is 10 bp, not the engine's 50 bp (`LIVE_MAX_SPREAD`): an order that
  crosses the spread pays half of it against the mid before any drift. With a 50 bp gate, any
  spread from 20 to 50 bp would pass the gate and be a guaranteed shortfall FAIL. The spread line
  is therefore at most 2× the shortfall line, and `buy` refuses above it before sending. VTI
  normally quotes 1–2 bp, so this only bites on a bad quote. A `sell` is never refused for a wide
  spread (exits always pass): if you must sell into one, the leg fails and the report says why.
  The engine's 0.5% guard is a separate, looser execution-safety line, not this measurement.
- The one check that is not our code: compare each session with Alpaca's trade confirmation /
  account statement (qty, price, fees) by hand and note it in the session log.
- Unfilled orders (steps 2 and 5) are listed but not measured. A partial fill that is then
  cancelled has no `filled_at` and fails the leg: partial fills belong in the paper stress pack.

**What a clean result means.** Execution only. Strategy signals go live only after the
DSR/PBO gate and the drawdown limit pass (section 8, phase 2).

**Rollback.** `touch ~/AETHER_HALT` (blocks canary buys too) → `python3 canary.py cancel` →
`python3 canary.py sell --limit <bid>` → `python3 canary.py assert-flat`. If the script or
server is unavailable, cancel and close VTI in the Alpaca web dashboard. Engine procedures:
section 6.

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
lists any open live positions. The US engine has no stop-loss orders or engine-managed stops
(stops exist only in the crypto strategy, which cannot trade live): live US positions are
changed only at the monthly rebalance, and new entries are stopped by the breakers. While the
engine is down nothing manages them at all. Optional off-VM dead-man ping via `HEALTHCHECK_URL`
(the engine and the watchdog both use it; `HEALTHCHECK_PING_URL` is accepted as a legacy alias,
since the engine and `setup_telegram.sh` used that name while the watchdog read only
`HEALTHCHECK_URL` — with only one set, the off-VM alarm was silently off for the other). Alpaca
keys are never entered through the dashboard: the `/api/settings/alpaca` route was removed
2026-10-17; credentials reach the server over SSH only.

- **24/7 operation (2026-10-18).** The engine restarts itself (Restart=always, watchdog 180 s) and starts at boot; timers use Persistent=true. `connectivity.py` checks internet, Cloudflare, the tunnel, the engine, Telegram, GitHub and Alpaca every 60 s; a link that fails two checks in a row raises a Telegram alert ("disconnected", with the fix) and a recovery message ("back after N min"). Telegram cannot announce its own outage, so that one is reported on recovery. Outage history is kept in `connectivity_events.jsonl` and every reply Telegram sends back in `telegram_link.jsonl` (no token, chat id or message text). A dead server cannot report itself: set `HEALTHCHECK_URL` in `.env` (healthchecks.io) so something outside the server alerts you. After any reboot run `bash ~/ratelark/deploy/post_reboot_check.sh`; every line should say OK. `CONNECTIVITY_ALERTS=0` turns the alerts off.
