# Pre-registration: tactical strategies on historical data (v1)

Registered 2026-10-22, before any historical intraday data was downloaded or looked at.
Tag: `prereg-tactical-hist-v1`. Owner request 2026-10-22 ("try to break every strategy before trusting it;
hundreds of historical trades, not 40").

## 1. What is tested

The two tactical paper strategies exactly as frozen in `aether_tactical/` on 2026-10-21. The runner imports
the engine modules themselves (`orb_engine`, `engulfing_engine`, `sim`), so the rules, fills and costs cannot
drift from the forward test:

- **A, `orb15`**: 15-minute opening range breakout on SPY, long only (rules in `orb_engine.py`).
- **B, `engulf15`**: 15-minute bullish engulfing scalp on SPY, long only (rules in `engulfing_engine.py`).
- Fills and costs as in `sim.py`: entry at the next 1-minute open, stop at min(bar open, stop), target at
  max(bar open, target), stop first if both in one bar, 2 bp per side.

Research only. Nothing here places orders or is imported by the engine. The forward 40-session paper test
(strategy.md, "Tactical paper tests") is unchanged and keeps running.

## 2. Data

- SPY 1-minute bars, Alpaca **SIP** feed (consolidated tape), raw prices, sessions from Alpaca's market
  calendar (early closes included).
- Window: **2016-01-04 to 2026-10-20**, the trading day before the forward test began. Nothing on or after
  2026-10-21 is read.
- SIP, not IEX: Alpaca's IEX history is short and IEX is a thin slice of volume. The forward test uses IEX,
  so this is a known difference, stated here rather than discovered later.
- One fetch. `fetch` refuses if the data file already exists. The file's SHA-256 is printed and stored with
  every logged trial.
- Each session goes through the engine's own `bars.verify`. A session that fails verification scores no
  trade (return 0) and is counted. If more than 2% of sessions fail, the run is refused as a data problem.

## 3. Variants (6, all logged)

| Strategy | Parameter | Baseline | Neighbours |
|---|---|---|---|
| A `orb15` | opening range length | 15 min | 10 min, 30 min |
| B `engulf15` | volume filter (x median of prior five 15-min bars) | 1.5 | 1.3, 2.0 |

Neighbours exist only to see whether a small change breaks the result. They are never candidates: the
baseline is the only variant that can earn a verdict, and no neighbour may replace it.

Every variant is logged once through `trials.log_trial` (batch `tactical-hist-v1`), winners and losers,
with its full daily return series (0 on days without a trade). Six more trials count toward N.

## 4. Measures

Per variant: trades, win rate, mean net bp per trade, t-stat (mean / (sd / sqrt(n)), the ledger's
formula), profit factor, total net return, max drawdown, annualised Sharpe of daily returns, net result by
calendar year. Daily return on a trade day = net bp / 10,000 (whole sleeve in one trade); 0 otherwise.

Cost stress: the baseline is also scored at **4 bp per side**. Reported and used in C6; not a new trial.

## 5. Criteria (baseline variant of each strategy)

| | Criterion | Pass if |
|---|---|---|
| C1 | Enough trades | trades >= 100 |
| C2 | Edge after costs | mean net bp > 0 **and** t-stat >= 2.0 |
| C3 | Survives small changes | both neighbours have mean net bp > 0 |
| C4 | Not one lucky stretch | net positive in >= 6 of the 10 full years 2016-2025 |
| C5 | Not luck among many tries | deflated Sharpe >= 0.95 |
| C6 | Survives worse fills | mean net bp > 0 at 4 bp per side |

C5: N = logged research trials + backfilled (untracked) trials + these six if not yet logged, floor 37
(31 from the daily-v1 registration + 6). Trial Sharpe variance per period = the surviving stage-1 record
used by daily-v1 (`HIST_VAR_PER_PERIOD`, 0.000324276...).

PBO is computed across each strategy's three variants and **reported only**: three variants are too few for
it to mean much.

## 6. Verdicts (fixed now)

- **Historically supported**: C1-C6 all pass.
- **Historically unsupported**: C2's mean net bp <= 0 (loses money after costs over the whole history).
- **Mixed**: anything else.

## 7. What a verdict does

- The forward 40-session paper test continues unchanged for both strategies, whatever this says. No rule,
  parameter or cost is changed because of this result; a changed rule is a new strategy with its own
  registration and its own 40 sessions.
- **New gate, added before any data is seen:** a live micro test may be proposed for a tactical strategy only
  if its forward verdict is "worth a live micro test" **and** its historical verdict here is "historically
  supported". Even then, going live still needs the owner's decision, the Risk Controller and the canary path.
- "Historically unsupported" is recorded in strategy.md. The forward test still runs to 40 sessions (the
  locked plan), but that strategy cannot go live under this registration.

## 8. Run procedure (on the server, in this order)

1. `python3 aether-research/tactical_hist/tactical_hist.py fetch` (once; paper data keys from `.env`).
2. `python3 aether-research/tactical_hist/tactical_hist.py run` (refused unless the registration tag is an
   ancestor of HEAD and tracked files are committed).
3. `python3 aether-research/tactical_hist/tactical_hist.py run --log-trials` (logs the six variants once;
   refused if batch `tactical-hist-v1` already exists).
4. Record both verdicts in strategy.md. No re-runs with different settings.
