# AETHER Live-Readiness Test Plan

Scope: US ETF engine, micro-live ($50-$100), human approval on entries, crypto live off.
Status: LOCKED v2.4 (amends v2.3, tag plan-v2.3). Commit to the repo BEFORE running any evidence test. Criteria may not be edited after results are seen (see section 12 for the only allowed way to amend).

## 0. Rules of the plan

1. **Fail closed.** Every failure test passes only if AETHER places no unintended order, raises a Telegram alert, and leaves correct state.
2. **One failed item = NO-GO.** No waivers after seeing results. A fix means the whole affected layer reruns.
3. **Evidence, not claims.** Each test stores: ID, commit SHA, date, command, raw output, PASS/FAIL, under `tests/evidence/<ID>/<YYYY-MM-DD>.json`. Tests are scripts, not manual impressions.
4. **Break it yourself first.** For each control, try to defeat it. If you cannot explain how it could fail, you have not tested it.
5. **Kill criteria are written before the first live dollar** (section 9).
6. **Prior passes do not count.** Earlier results (kill switch, rejected-order test, live reconciliation, `test_uslive.py` 23/23) are re-run under this plan to capture commit SHA and stored evidence.
7. **Approval scope.** Human approval gates ENTRIES only. Exits, stops, and halt-driven flattening run automatically and must never wait for Telegram, approval, or the HALT file.
8. **Traceability.** Every order and every decision-log row carries strategy version, commit SHA, config hash, and `client_order_id`.

---

## 1. Strategy truth (does an edge exist at all?)

| ID | Test | Pass criterion |
|----|------|----------------|
| S1 | Pre-register, then run Tier A via import_strategy.py | Prereg commit timestamp precedes all results |
| S2 | DSR with full N (incl. 24 backfilled estimates + all variants) | DSR >= 0.95 |
| S3 | PBO (CSCV) | PBO <= 0.05 |
| S4 | Holdout, single use | Net-positive AND holdout Sharpe >= 0.5 x in-sample Sharpe; lock then burned |
| S4b | Holdout lock enforcement: attempt a second holdout run | Code refuses; attempt is logged |
| S5 | Cost stress x2 and x3 (fees, SEC/TAF sell fees, slippage, spread on IEX quotes) | Net expectancy stays > 0 at x2 |
| S6 | Parameter neighborhood: every param +/-20% | >= 80% of neighbors stay net-positive |
| S7 | Placebo: 1,000 random-signal strategies through the same gate | <= 5% pass (else gate too loose) |
| S8 | Shuffled-returns null: block-shuffle returns, rerun | Strategy edge disappears on shuffled data |
| S9 | Time-shift test: delay signals 1, 2, 5 days | Edge decays smoothly; a huge cliff at +0 means lookahead |
| S10 | Block bootstrap (21-day blocks, 5,000 draws) | 5th-percentile Sharpe > 0 |
| S11 | Monte Carlo drawdown (10,000 trade-order permutations) | 95th-percentile max DD within strategy.md limit |
| S12 | Regime slices: 2008, 2011, 2015-16, 2018Q4, 2020, 2022 | No slice breaches max-DD limit; no hidden single-regime dependence |
| S13 | Leave-one-year-out | No single year contributes > 40% of total profit |
| S14 | Data cross-check: yfinance vs Alpaca (IEX) closes, over the overlap period only | Median diff < 5 bps; signals agree >= 95% of days. Pre-overlap slices (S12) are marked "single-source data" |
| S15 | Corporate-action audit (splits, dividends) on every ETF | Adjusted vs unadjusted returns reconcile |
| S16 | Reproducibility: rerun any trial from stored hash + commit + pinned requirements | Returns byte-identical (SHA-256 equal) |
| S17 | Benchmark: vs buy-and-hold SPY and vs 60/40 | Better risk-adjusted (Sharpe or Calmar), not just return |
| S18 | Trade count | >= 100 round trips, or the claim is marked "insufficient evidence" |
| S19 | Kill-rule calibration: from S10 bootstrap, frequency of any 20-trade window with PF < 1.0 | If > 20% of windows, the PF rule in strategy.md must be amended (section 12) before L1 |
| S20 | Live-size feasibility: rerun sizing at $50-$100 capital with fractional rounding and Alpaca minimum notional | No signal sized to zero or below minimum; tracking error vs full-size backtest documented |

## 2. Code correctness

| ID | Test | Pass criterion |
|----|------|----------------|
| C1 | **Lookahead detector:** truncate data at day t, compute signal; compare with full-data signal for day t | Identical for every t in sample |
| C2 | Determinism: same inputs twice | Identical orders and state |
| C3 | Property tests (hypothesis lib): weights sum <= 1, no NaN, no negative cash, sizes <= limits for random inputs | 10,000 cases, 0 violations |
| C4 | **Mutation testing on risk_controller.py** (mutmut): mutate each limit/comparison | Every mutant is killed by a test; any survivor = missing test |
| C5 | Fuzz order inputs: qty 0, negative, NaN, inf, huge, wrong symbol, wrong side, below minimum notional, excess decimal places | All rejected, never crash into submitting |
| C6 | Replay test: feed 1 year of recorded bars through the live code path | Decisions match backtest 100%, or every mismatch has a written root cause |
| C7 | Time handling: naive vs aware datetimes, UTC vs ET, DST, leap day | No order on wrong-day signals |
| C8 | Limit-price computation: wide spread, zero bid/ask, crossed quote, stale quote | Price within max-deviation of last trade, or no order |
| C9 | `client_order_id` construction | Deterministic from (date, symbol, side, strategy version); unique across all decisions in replay |
| C10 | Config drift: change `.env` while running | Takes effect only on restart; config hash logged at startup and on every decision |
| C11 | Indicator correctness for the US ETF rotation (us_strategy.py has no SMA/ATR): momentum_score matches an exact-arithmetic (Fraction) reference on 50 random series; known-answer cases on target_weights (all-positive, all-negative -> 100% cash ETF, flat -> cash ETF, mixed -> 1/3 cash ETF, ties alphabetical, missing or short history raises, weights sum to 1); plan_orders invariants (never oversell, buys <= cash + proceeds, sells before buys, at-target -> no orders) | tests/test_c_layer.py C11 and plan_orders tests all pass; momentum abs difference <= 1e-12 |
| C11b | Real-data repeat of C1 and C11: stored real adjusted closes (snapshot from fetch_us_daily, SHA-256 recorded) for EVERY month-start in the backtest sample | Truncated-data signal equals full-data signal at every month-start; momentum matches the exact reference to 1e-12; weights equal the stored backtest signal for that month |
| C12 | Signal-to-submit timing: measure with `time.monotonic()` (not wall clock) and write to the decision log: engine_delay = (risk check done - signal generated) + (submit - approval received; for exits, approval time = risk check done). Human approval wait is excluded | Engine delay p95 < 60 s and never > 5 min; warning logged at 60 s; every outlier has a root cause; every decision-log row has engine_delay_s |

## 3. Failure injection (chaos)

Run on paper account. For each, record: order sent? alert? state correct? recovery automatic?

**3a. Crash points.** `kill -9` the engine at EVERY step: after signal, after risk check, after Telegram approval, after submit but before ack, after ack but before state write, mid state write.

| ID | Test | Pass criterion |
|----|------|----------------|
| F1 | Crash at each of the 6 points above, restart | Never a duplicate order; state reconciles to Alpaca |
| F2 | Idempotency: every order carries a deterministic `client_order_id` | Resubmitting same decision is rejected by Alpaca |
| F3 | Delete all JSON state, restart | Rebuilds from Alpaca; no orders sent during rebuild |
| F4 | Corrupt JSON state (truncate, garbage) | Refuses to trade, alerts, rebuilds from Alpaca |
| F5 | Manual order placed in Alpaca UI | Detected within one cycle; entries halt until reconciled |
| F6 | Manual position closed in Alpaca UI | Detected; engine does not "re-sell" a position it no longer holds |
| F7 | VM hard reboot mid-session | Service auto-starts; reconciles; no duplicate |
| F8 | Disk full / read-only filesystem | Fail closed + alert; no half-written state |
| F9 | Clock skew +/-5 min and +/-1 hour; NTP sync disabled | Refuses to trade; alert |
| F10 | **Dead-man's switch:** engine hung or VM down (it cannot alert about itself) | External heartbeat check (off-VM) alerts within 10 min |
| F11 | Log growth: 90 days of logs simulated | Rotation works; disk usage bounded |
| F12 | Alert storm: 100 errors in 1 minute | Alerts rate-limited and summarized; Telegram not banned; first alert still delivered |

**3b. Data faults.**

| ID | Test | Pass criterion |
|----|------|----------------|
| D1 | Block data feed (iptables) | Stale-data guard rejects new entries; exits still allowed |
| D2 | Last bar older than strategy threshold | No entry |
| D3 | Bad tick: price 0, NaN, negative, +/-10x spike | Rejected, alert, no order |
| D4 | Missing bar in middle of series | Detected; no signal computed on gapped data |
| D5 | Duplicate / out-of-order bars | Deduplicated or rejected |
| D6 | IEX vs consolidated divergence injected | Limit orders protected by max-deviation check |
| D7 | Symbol halted / delisted / renamed / ETF closure announced | No order; alert |
| D8 | Split announced for held ETF | Position and stop levels adjusted or trading halted |
| D9 | Cash dividend paid on held ETF | Reconciliation explains cash change; no false mismatch alarm |
| D10 | Rebalance-time validation failure (stale, bad close, gap, duplicate, spike, short history) | us_cycle places no orders and sends an alert, verified with an injected fake feed and the real alert path; tests/test_data_layer.py (D2-D5) passes |
| D10b | Hourly signal-preview failures (invisible while the market is closed: the status note is overwritten) | Surfaced by the daily real-data check (A2), which alerts on any validation failure the same day |

**3c. Broker and network faults.**

| ID | Test | Pass criterion |
|----|------|----------------|
| B1 | Alpaca 429 / 500 / 503 / timeout | Bounded retries with backoff; no duplicate on retry |
| B2 | Timeout AFTER order accepted (ack lost) | Engine queries order by client_order_id before retrying |
| B3 | Order rejected (insufficient settled cash, PDT, fractional rule, market closed) | Logged, alerted, no retry storm |
| B4 | Partial fill, then cancel | Position/stop sized to actual filled qty |
| B5 | Limit order never fills; day order expires overnight | Cancel after timeout; no orphan open orders next session |
| B6 | Websocket/stream drop | Falls back to polling or halts |
| B7 | Broker returns unexpected schema | Fails closed, does not crash into default values |
| B8 | DNS failure / full network loss for 10 min | Halts entries; resumes safely |
| B9 | **Good-faith violation:** sell, then a buy signal before proceeds settle | Engine refuses the buy itself (settled-cash check), does not rely on broker rejection |

**3d. Control-plane faults.**

| ID | Test | Pass criterion |
|----|------|----------------|
| T1 | Telegram down | No approval -> no ENTRY; queued approvals expire; exits still execute |
| T2 | Approval replay (same message tapped twice, or old approval reused) | Single use; expired after N minutes |
| T3 | Approval from wrong chat ID | Ignored |
| T4 | Approval arrives after price moved past tolerance | Order re-checked against current price, rejected if outside |
| T5 | `touch ~/AETHER_HALT` mid-session | All entries stop within one cycle; exits and alerts still work |
| T6 | Order 10x over `RISK_MAX_ORDER_USD_LIVE` | Blocked |
| T7 | Daily buy cap reached, then retry across restart | Cap persists across restart |
| T8 | Approval requested outside market hours or while you are asleep (UAE evening/night = US session) | Expires cleanly; no stale approval executes next session |
| T9 | Approval message content | Shows symbol, side, qty, limit price, notional, account (paper/live), expiry; wrong or missing field -> not sendable |

**3e. Mode isolation.**

| ID | Test | Pass criterion |
|----|------|----------------|
| M1 | Paper mode with live keys present in env | Startup refuses |
| M2 | Live mode with paper keys | Startup refuses |
| M3 | Account-ID check at startup and before every order | Mismatch -> halt |
| M4 | Both LIVE_TRADING=true and wrong base URL | Refuses |
| M5 | Research env with live keys | Research scripts refuse to run (already coded) |
| M6 | Dry-run: grep logs/records after a paper session | Zero live-account order IDs |
| M7 | Crypto live path with LIVE_TRADING=true for US | Crypto and webhook live orders still blocked |

**3f. Market calendar.**

| ID | Test | Pass criterion |
|----|------|----------------|
| K1 | Holiday | No signal-driven orders |
| K2 | Half-day (day after Thanksgiving, Christmas Eve) | Orders respect early close |
| K3 | DST change weekends (UAE has none; US does) | Schedule still fires at correct ET time |
| K4 | Market-closed submission | Rejected or queued deliberately, never silently |
| K5 | Current-month and partial bars cannot reach the signal: inject a +10% bar dated on the signal day and an absurd bar elsewhere in the current month, on an EDT date (2026-07-15) and an EST date (2027-01-13), plus month-boundary cases (first of month excluded, month-end bar kept). The price used for order limits must also pass the C8 max-deviation check, so a stale after-hours print cannot set a limit | Weights identical to a control run without the injected bars (tests/test_c_layer.py K5); C8 passes |

**3g. Exits and drawdown protection (the US rotation engine has no per-position stops by design; strategy.md must say so).**

| ID | Test | Pass criterion |
|----|------|----------------|
| E1 | Exit never blocked at the executor: HALT file present, drawdown kill active, daily caps hit, Telegram down; a manual closing SELL is sent. The US engine has NO automatic exit path (rotation only), so this is a manual-exit guarantee | Sell reaches the broker (test_halt_allows_exit.py); entries stay blocked; the kill alert says nothing sells automatically |
| E2 | Rotation is all-or-nothing: a plan whose buys would be refused (halt appears mid-plan, per-order cap, daily budget) | No order is submitted (risk_controller.preflight), so sells never liquidate without the replacement buys |
| E3 | Drawdown kill switch (US_MAX_DRAWDOWN_PCT, default 27): equity paths just below and just above the limit; recovery after a trip; engine restart after a trip; positions closed by hand after a trip | Below the limit entries are allowed; at or above, entries and rotations are blocked with an alert; the trip is latched in state and survives recovery, restart and manual closes; it clears only by the manual procedure (stop the service, written review, null us.dd_latched AND re-baseline us.equity_history); the 3% daily-loss kill still clears itself; unknown data never clears a trip (tests/test_kill_latch.py) |
| E3b | State lost: delete or corrupt aether_state.json while the paper account holds positions (with F3/F4) | The engine refuses to take over unknown positions and alerts; the equity peak and the latch come back from the latest backup or are re-baselined only by the written-review procedure; a lost peak must not silently re-arm entries |
| E4 | Engine down across a month-end and first trading day, then restart | The rebalance runs exactly once after restart: not missed, not duplicated |
| E5 | Crash mid-rotation (kill -9 after some sells filled, before the state save), then restart, on the paper account | No duplicate order; the engine reconciles to Alpaca; paper-sleeve cash and equity are not understated (no false daily-loss or drawdown kill), or the engine halts with an alert and the runbook fixes it; never doubles an order |
| E6 | strategy.md versus code | strategy.md states the US engine has no per-position stops; every statement about stop-loss or stop-protected positions is corrected or scoped to the crypto engine; doc and code agree |
| E7 | A SELL that is rejected, canceled, errored or only partly filled during a rotation | The buys are skipped (skipped_after_failed_sell), the month stays incomplete, an alert is sent, and the retry waits US_RETRY_SECONDS (tests/test_rotation_safety.py) |
| E8 | Retry after a partial fill or cancel in the same month (client_order_id is one per symbol, side and month) | A retry never re-reads the old order as new and never double-books its fill; the missing quantity is bought or the engine halts with an alert; verified on the paper account |

## 4. Security red team

| ID | Test | Pass criterion |
|----|------|----------------|
| X1 | Forged TradingView webhook (no secret, wrong secret, old secret) | 401, no order, alert on repeats |
| X2 | Webhook replay (valid payload re-sent later) | Rejected (timestamp/nonce) |
| X3 | Malformed/oversized webhook payload | Rejected, no crash |
| X4 | Dashboard: brute-force login, session fixation, no-cookie access | Locked out / denied |
| X5 | Port scan the VM from outside | Only intended ports open; 8000 is localhost only |
| X6 | Secrets scan (trufflehog/gitleaks) on repo AND history | Zero secrets |
| X7 | `.env` permissions and backups encryption | 600; backup SAS is Create-only (verify you cannot read/list with it) |
| X8 | Alpaca live key scope and IP restrictions (if available) | Minimal scope |
| X9 | Dependency audit (pip-audit) | No known critical CVEs |
| X10 | LLM isolation: confirm the lab and any LLM code cannot import engine or read live keys | Import test + env test |
| X11 | External strategy catalog/import path isolation | Imported code can only reach `trials.log_trial`; cannot import engine, read `.env`, or open network to Alpaca |
| X12 | Cloudflare Access on ops dashboard | Unauthenticated and non-allowlisted email denied; SSH tunnel fallback still works |
| X13 | Secrets in logs, Telegram messages, and decision log | Zero keys, secrets, or full account numbers |

## 5. Backup and recovery drills

| ID | Test | Pass criterion |
|----|------|----------------|
| R1 | Restore from offsite backup onto a FRESH VM, using only the repo + password manager | Engine runs and reconciles within 60 min |
| R2 | Restore with the newest backup corrupted | Falls back to previous |
| R3 | Lose the VM entirely mid-position | Positions have exits or are manually closable from phone; runbook works |
| R4 | Rotate every secret (webhook, dashboard, keys, backup SAS) | System back up with no stale credentials |
| R5 | Backup failure (SAS expired, storage unreachable) | Alert within one backup cycle |

## 6. Soak test (paper: minimum 30 calendar days AND at least two month-end rebalance executions)

Run continuously with no code changes. Any code change restarts the clock.

- **Daily reconciliation job:** Alpaca positions, orders, fills vs AETHER log. Zero unexplained differences.
- **Per-fill comparison (primary):** replay each day through the backtest code; compare entry/exit price, slippage, missed trades.
- **Minimum sample:** if fewer than 10 paper fills occur in the soak, extend it until 10 fills. The soak must also contain at least two rebalance executions (the strategy rebalances monthly).
- **Pass:** average slippage <= modeled cost; zero missed trades without written cause; zero duplicate orders; zero unalerted errors; uptime >= 99% of scheduled cycles (cycles that ran / cycles scheduled).
- **Secondary:** realized net return within the simulation's 90% confidence band (reported, low statistical power at this sample size).
- **Weekly:** unannounced drill (random fault from section 3, chosen by a script, not by you).
- **Daily automation (11a) and daily recovery checks (11b) run every day of the soak.** A missing daily heartbeat counts as a failure.

## 7. Game days (unannounced)

Once per week during soak, a script picks one scenario from sections 3-5 and triggers it at a random time. You do not know which. Pass = alert received within 2 minutes (10 minutes for F10) and correct automatic behavior.

## 8. Live ramp (human approval on every entry)

| Stage | Size | Duration | Advance only if |
|-------|------|----------|-----------------|
| L0 | $0 (live account funded, orders disabled) | 1 week | Live reconciliation job clean; heartbeat (F10) live |
| L1 | $50-$100 total across the full rotation (up to 3 ETFs, fractional) | >= 20 closed trades (expect well over a year at monthly turnover; S18 must report the backtest's closed trades per month so the duration is known before L1) | Fills match paper within tolerance; zero incidents |
| L2 | 2x | >= 20 more trades | Same, plus drawdown within limits |
| L3 | Further steps | Per strategy.md | Each step needs written review |

- Paper keeps running alongside live at every stage.
- Before L1, confirmed with Alpaca in writing: cash vs margin account, PDT handling, fractional limit-order support.
- At trend-following pace, L1's 20 trades may take several months. This is expected and is not a reason to raise size early.

## 9. Kill criteria (fixed now, not later)

Halt live entries immediately and require written review if ANY occur:
- Drawdown > 2x out-of-sample worst (-27%): automated entry block by the drawdown kill switch (E3). PF < 1.0 over the last 20 closed trades: manual month-end review per strategy.md (S19 confirms or amends the threshold before L1). Either one requires written review before entries resume.
- Any duplicate order, wrong-account order, or order outside limits.
- Any reconciliation difference unresolved after one cycle.
- Realized slippage > 2x modeled over 20 trades.
- Any unexplained engine restart, unalerted error, or missed heartbeat, or deployed code (code_version) that differs from git HEAD.
- Any stop that failed to execute when breached.

## 10. Go / No-Go scorecard

| Layer | Items | Required |
|-------|-------|----------|
| 1 Strategy truth | S1-S20 (incl. S4b) | all |
| 2 Code correctness | C1-C12 (incl. C11b) | 13/13 |
| 3 Failure injection | F, D, B, T, M, K, E | all |
| 4 Security | X1-X13 | 13/13 |
| 5 Recovery | R1-R5 | 5/5 |
| 6 Soak | 30 days, two month-end rebalances, >= 10 fills, criteria met | yes |
| 7 Game days | all passed | yes |
| 8 Daily automation and recovery | A1-A6, RD1-RD5 | green on every day of the soak; a red day is an incident |

NO-GO unless every row is satisfied and signed with evidence links.

## 11. Known limits of any test plan

- Backtests and paper cannot reproduce live market impact, queue position, or your own psychology under real loss.
- Passing everything shows the known risks are controlled. It does not prove profit. Position size stays small precisely because of this.
- Re-run layers 1-3 after every material code or strategy change.

## 11a. Daily automation (starts with the soak)

One systemd timer (aether-daily.timer, 04:30 UTC, after the nightly backup), run as azureuser. It never restarts, stops or starts a service, never touches the halt file, never uses the live keys, and never places an order: it uses the strategy paper keys read-only for A2 and A3, and unit tests replace Telegram with fakes. Every run writes tests/evidence/daily/<YYYY-MM-DD>.json (commit SHA, results, hash of the results), sends one Telegram summary line, and pings its own off-VM check (DAILY_HEALTHCHECK_URL, 1-day period). A missing run is a failure. The evidence folder is not committed (a commit would move HEAD) and is included in the nightly backup.

| ID | Check | Pass criterion |
|----|-------|----------------|
| A1 | Regression suite: tests/ (all), test_uslive.py, test_halt_allows_exit.py | All pass at the deployed commit |
| A2 | Real-data check (read-only): fetch_us_daily for the universe, validate_bars, us_signals dry run for the current month | No ValueError; newest completed-month bar within MAX_STALENESS_DAYS; the current target weights are logged |
| A3 | Reconciliation: Alpaca positions and open orders versus aether_state.json (paper) | Zero unexplained differences |
| A4 | Version and tree: the engine code (every .py file except tests, the stress script, research code, patches and the daily runner) at the commit named by code_version in the newest decision-log row equals the same code at git HEAD, and git status is clean | Equal and clean. Commits that change only docs, tests, research, patches or the runner are not a mismatch; anything else is an unexplained change |
| A5 | Stress test on the dedicated stress account (stress_test.py), switched on with AETHER_DAILY_STRESS=1 once the script has been reviewed | Passes; never touches the strategy account or the halt file. NOT ENABLED is not green and must be resolved before the soak starts |
| A6 | Runner heartbeat: summary sent and off-VM ping received | A missing day is a failure |

## 11b. Daily recovery checks

Non-destructive: restores go into a temporary folder, never over live files.

| ID | Check | Pass criterion |
|----|-------|----------------|
| RD1 | Newest local backup archive | Under 26 h old; size within 50% of the median of the last 7; 14-day rotation intact |
| RD2 | Archive re-opened: extract the newest archive into a temporary folder | tar extracts; both SQLite files pass integrity_check; aether_state.json parses and has the us_paper and us_live keys; git_commit.txt present |
| RD3 | Offsite upload | The aether-backup.service log shows "uploaded <archive>" for the newest archive within 26 h |
| RD4 | Recovery configuration | aether.service has Restart=always and WatchdogSec set; HEALTHCHECK_URL is set; the backup, watchdog and health timers are active |
| RD5 | Backup coverage | The archive holds every file needed to resume safely: aether_state.json, .aether_risk_state.json, live_risk_ledger.json (once it exists), the decision log, research.sqlite, git_commit.txt, and tests/evidence (once it exists), so the plan's own evidence survives losing the VM |

Monthly, by hand, with the evidence stored: R1 fresh-VM restore (60-minute target); R2 corrupted-newest fallback; offsite readability check with a read-capable credential kept off the VM (the VM's upload credential is Create-only); F10 drill (stop the watchdog timer for 15 minutes; the external alert must arrive within 10 minutes).

## 12. Amendments

- The plan may be changed only by a commit made BEFORE the next test run, with a written reason in the changelog below.
- Any amendment that touches a layer resets that layer: all its items rerun from scratch.
- Loosening a criterion after seeing a failing result is not allowed under any wording.
- Development runs (tests run while writing or fixing code) are not evidence. Only runs of committed test scripts at a clean commit, with stored evidence per rule 0.3, count.

### Changelog

| Date | Commit | Change | Reason |
|------|--------|--------|--------|
| | | v2: added S4b, S19, S20, C8-C10, F10-F12, D9, B9, T8-T9, M7, section 3g, X11-X13, R5, rules 0.6-0.8, section 12; tightened S4, C6, soak criteria | Review before first run |
| | | v2.1: added C11 (indicator correctness), C12 (signal-to-submit timing), K5 (after-hours spike) | Gaps found vs external checklist |
| | | v2.1 spec clarifications: C11 full-series comparison, 1e-9 SMA tolerance, ATR warm-up of 5 x period, real engine functions; K5 timezone-safe (zoneinfo, EST and EDT) with control-run identity check; C12 monotonic clock, approval wait excluded, 60 s warning (not 5 s), delay recorded in decision log | Pasted specs had loose tolerance, wall-clock/DST errors, and would have counted approval wait |
| | | v2.1 patch 2: C11 warm-up set to 200 bars on >= 500-bar series (5 x period was too short for 1e-6), ATR rtol 1e-6, real ETF data and real us_strategy.py functions required; K5 adds fresh-state-per-run, EST and EDT dates, 15:55 positive control, caplog drop-message assertion | Warm-up math error; K5 test could not detect a spike or prove isolation |
| 2026-10-07 | 2da6fe1b19eb58cbda8dda3134426f682d882cb0 (tag plan-v2.1) | v2.1 locked: this commit fixed the criteria; earlier changelog rows are drafts of the same file | Commit and tag pushed to origin before any test run |
| 2026-10-07 | 8f139232e73fbde4d428f596c3b5bce6a969a7df (tag plan-v2.2) | v2.2: C11 retargeted to the real monthly-rotation code (no SMA/ATR exist); C11b real-data repeat of C1/C11; K5 retargeted to current-month and partial bars (same EST/EDT dates); 3g retargeted from stops to exits, drawdown kill and rotation safety (E1-E6); D10 added; soak must span two month-end rebalances; L1 sized as the full rotation; PF rule is a manual month-end review per strategy.md and the drawdown rule is automated; section 12 states development runs are not evidence | Plan assumed a daily trend strategy with stops; reading us_strategy.py, strategy.md and fetch_us_daily showed otherwise. No evidence test had been run. Development runs are in the commit history (d1eac5f, 15540b4). The only criterion removed is the SMA/ATR check, which cannot apply to this strategy. |
| 2026-10-07 | b86646355e6cf17359091a2d78d77694dfdc883c (tag plan-v2.3) | v2.3: E1 reworded (the US engine has no automatic exit), E3 latched drawdown kill, E3b/E5/E7/E8 added or rewritten for the rotation engine, D10 split into the rebalance path and a daily preview check (D10b), new sections 11a daily automation and 11b daily recovery checks, scorecard layer 8, kill criterion on code/HEAD mismatch | Findings from reading _us_rebalance, _us_execute_paper, the kill-switch code and the backup script; code fixes in commit 0399606 (rotation safety, latch). No evidence test had been run. |
| 2026-10-07 | 073317fd306ea90f5fb15f2b781fd7ee7007559f (tag plan-v2.4) | v2.4: A4 compares engine code only (doc- or test-only commits are not a mismatch), A5 gated until the stress script is reviewed, 11a states that the runner reads with the strategy paper keys and is read-only, RD5 adds tests/evidence | Found while writing aether_daily.py: a literal 'HEAD equals code_version' would go red on any doc commit, and 'never uses the strategy keys' contradicted A2 and A3. No evidence test had been run. |
