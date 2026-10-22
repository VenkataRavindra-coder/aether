# Intraday manifest for the daily candidates (Amendment 1, section 2)

Written by `aether-research/us_momentum/fetch_0945.py` on the server. Commit this file BEFORE any run: the
runner refuses a data file whose SHA-256 differs from the one below. Coverage only; no return or performance
figure was computed.

sha256: bca9579ee7632f1b445fda7186ff9d971e829012c2f6273564a3f164824d2736
file: aether-research/us_momentum/data/intraday_0945_sip.csv.gz
overlap_start: 2016-01-04
window_end: 2026-10-06
sessions: 2705
feed: SIP (Amendment 3), Adjustment.ALL, 5-minute bars 09:45/09:50/09:55 ET + daily open

| symbol | first 09:45 bar | 09:45 coverage | 09:50 | 09:55 | daily open | none |
|---|---|---|---|---|---|---|
| SPY | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| QQQ | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| IWM | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| EFA | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| TLT | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| GLD | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| IEF | 2016-01-04 | 99.96% | 1 | 0 | 0 | 0 |
| SHY | 2016-01-04 | 99.93% | 2 | 0 | 0 | 0 |

A symbol below 98% 09:45 coverage makes every run that needs it INSUFFICIENT on criterion 5.
