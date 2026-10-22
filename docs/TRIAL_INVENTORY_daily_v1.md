# Trial inventory for the daily candidates (Amendment 1 section 4, Amendment 2 section 3)

Written 2026-10-22, before any result for S-A, S-B or S-C exists (`run` is still locked). It lists every strategy
attempt in AETHER's history OTHER than S-A, S-B and S-C, so the deflated Sharpe knows how many tries came before them.
The runner reads the `N_inventory:` line below and uses N = max(31, database rows + backfilled + candidates not yet
logged, N_inventory + 3).

## 1. Trial database on the server (`aether-research/research.sqlite`)

Expected output of `daily_candidates.py count` on 2026-10-22 (to be confirmed on the server before this file is merged):

| Source | Count | Note |
|---|---|---|
| Backfilled family `us_momentum_pre_hetzner` | 28 | Recorded total from before the Azure VM was lost; rows unrecoverable (Amendment 2 section 1) |
| Logged, batch `tactical-hist-v1` | 6 | `tactical_orb15`, `tactical_orb10`, `tactical_orb30`, `tactical_engulf15_v1.5`, `tactical_engulf15_v1.3`, `tactical_engulf15_v2.0` (2026-10-22, `docs/PREREG_tactical_hist_v1.md`) |
| Logged, `daily_v1_*` | 0 | The three candidates are not yet logged and are not part of N_inventory |

## 2. Attempts outside the database

| Attempt | Counted | Basis |
|---|---|---|
| `daily_edges_backtest.py`: mean reversion SPY, mean reversion QQQ, turn-of-month SPY | 3 | Unknown whether it ever ran. Counted as additional (Amendment 2 documented assumption) |
| `zone_reversal` (QQQ, BTC/USD) | UNKNOWN | Logs to its own `research_trials` writer; how many runs, and whether they are inside the 28, cannot be recovered |
| Nightly LLM hypothesis lab | UNKNOWN | No code in the repository; weak evidence it never ran, not proof |
| Crypto engine strategy (`strategy.py`, ATR exits) | UNKNOWN | Ran on paper before Oct 9; any backtests belong to the lost Azure history |
| Gold EMA-pullback lab (separate project, XAU/USD) | UNKNOWN | Kept outside AETHER; listed because S-A holds GLD |
| Tactical forward paper tests (orb15, engulf15) | 0 extra | Same hypotheses as the six logged variants; a forward test is not a new selection |
| Trend Gate (SPY vs 200-day SMA) | 0 | A live rule chosen from public evidence; never backtested or tuned here |
| Shared posts and prompts (24/7 Trading Team, Jev, quantscience TLT thread, others) | 0 | Never implemented as backtests; S-C's idea is counted once, as S-C |

## 3. Total

28 backfilled + 6 logged + 3 `daily_edges_backtest.py` = 37. UNKNOWN items are not counted as zero in spirit: the
runner's N-doubled stress scenario (Amendment 2) shows how fragile a result would be to an understated N.

N_inventory: 37

With the three candidates added as a constant, N = max(31, 6 + 28 + 3, 37 + 3) = **40**.
