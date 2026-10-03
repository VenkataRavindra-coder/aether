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

---

## 7. Operations

- **Access:** `https://ops.ratelark.app` (Cloudflare Access email code + `DASHBOARD_PASSWORD`).
  Fallback: `ssh -N -L 8000:127.0.0.1:8000 aether` then `http://127.0.0.1:8000`. `/logout` requires
  a valid session and only accepts `POST` (it used to be a public `GET`, so anyone on the internet
  could call it and force every operator session to log out, repeatedly).
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
