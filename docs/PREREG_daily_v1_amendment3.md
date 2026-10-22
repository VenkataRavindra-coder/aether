# Amendment 3 to `docs/PREREG_daily_v1.md`: criterion 5 uses the SIP feed

Status: LOCKED at the commit tagged `prereg-daily-v1.3`. Written 2026-10-22, before any result for S-A, S-B or S-C
exists: `run` is still locked (`RUN_LOCKED = True`), and no return, Sharpe or other performance figure has been computed
from any 09:45 data. Everything not mentioned here is unchanged, including Amendments 1 and 2.

## 1. What the coverage report showed

The first 09:45 fetch (server, 2026-10-22, IEX feed, file SHA-256 `a6ac7911dac67b866c386619304d7a94625c993170b9d9e8ffb5d8297b141eaa`)
printed coverage only, as Amendment 1 requires. Overlap start 2020-07-30, 1,554 sessions. Share of sessions with a
09:45 bar:

| SPY | QQQ | IWM | EFA | TLT | GLD | IEF | SHY |
|---|---|---|---|---|---|---|---|
| 99.94% | 99.94% | 99.87% | 98.71% | 98.84% | **93.82%** | **86.68%** | **50.39%** |

SHY is the cash leg of all three candidates and IEF is half of the 60/40 benchmark, so under the 98% rule criterion 5
would be INSUFFICIENT for every candidate by construction. The gate could not do its job: it would be decided by the
feed's thin trading in quiet ETFs, not by execution price. IEX is a small slice of volume; the consolidated tape (SIP)
records every venue.

## 2. The change

Criterion 5's data comes from Alpaca's **SIP** feed instead of IEX. Everything else in Amendment 1 section 2 is
unchanged: the 09:45 / 09:50 / 09:55 five-minute opens and the daily open as last resort, `Adjustment.ALL`, the 98%
coverage and 2% substituted-fill limits (per run, never pooled), the 0.75x Sharpe ratio, criterion 4 at 09:45, the
756-day and 30-round-trip minimums, the 2026-10-06 cut, one fetch, and a committed manifest whose SHA-256 the runner
checks. The overlap start is whatever the SIP coverage report shows; it is not chosen after seeing results.

The holdout execution path (Amendment 1 section 3) uses the same SIP feed.

## 3. Record

The IEX file and its coverage report are kept on the server for the record and are never read by the runner. Its
manifest was not committed. If the SIP coverage also leaves a needed symbol below 98%, criterion 5 is INSUFFICIENT
for the runs that need it, as registered; no further feed change is made for that reason.
