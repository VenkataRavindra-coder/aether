# AETHER research: zone reversal v1 (QQQ, BTC/USD)

Research only. Nothing here places orders. Every run is logged as a trial in `research.sqlite`
(`research_trials` table) so it can feed the deflated-Sharpe panel.

## Rules (long; short is the exact mirror)

| Step | Definition | Param (default) |
|---|---|---|
| Zones | 15-min swing highs/lows (pivot, k bars each side) from the last N days, using only bars that close before the cutoff (09:30 ET, or 00:00 UTC in BTC all-hours mode). Each level ± w; overlapping zones merge. | `zone_lookback_days` 5, `zone_pivot_k` 2, `zone_width_atr` 0.25 × 15-min ATR |
| Fast move | Over the last L one-minute bars, price fell at least X × ATR from the close L bars ago into the zone. No close bounced more than Y of the move above the running low. The move must start above the zone; the low may pierce up to one zone width below it. | `fast_lookback` 15, `fast_min_atr` 3.0, `fast_max_pullback` 0.3 |
| Trend break | A close above the highest high of the prior B bars, after the touch. | `break_lookback` 5 |
| Higher low | After the break, a bar whose low is ≤ the previous bar's low and below the next K bars' lows, and above the touch low. It's confirmed only after those K bars close. | `hl_confirm_bars` 2 |
| Strong candle | Bullish, body ≥ 60% of range, range ≥ 1 ATR, closing above the prior bar's high. | `candle_body_min` 0.6, `candle_range_min_atr` 1.0 |
| Entry | Next bar's open, plus slippage. | `slippage_bps` |
| Stop / target | Higher-low minus 0.1 ATR; target at 2R. If both are touched in one bar, the stop is assumed. | `stop_buffer_atr` 0.1, `target_r` 2.0 |
| Resets | New low below the touch low → back to "touched". Close more than one zone width through the zone, or 30 bars since the touch → setup cancelled. | `setup_timeout_bars` 30 |

## Modes

- `us_open`: setups are detected from 09:30 ET. Entries fill only between 09:45 and 10:00 ET. Time exit at 12:00 ET. Signals outside the window are counted as `outside_window` skips, which is the comparison for "timing vs pattern."
- `all_hours` (BTC): entries at any time, with a time exit after 240 bars. For stocks this means the whole regular session, exiting at the close.

## Sizing and costs (no compounding)

- Risk is fixed at 1% of the fixed allocation ($5,000 → $50). The quantity is capped at `equity × max_leverage` notional.
- `pct_capped` and `avg_risk_actual` show how often that cap cut the real risk below $50.
- Stock defaults: no commission, 1 bp slippage per side, shorts allowed, PDT limit on (max 3 day trades per rolling 5 sessions under $25k).
- Crypto defaults: long-only (Alpaca crypto can't be shorted), 0.25% fee per side, 5 bp slippage. Check your actual fee tier.

## Run

```bash
pip install alpaca-py pandas numpy
export APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...     # paper keys are fine; BTC data needs none

python reversal_backtest.py fetch --symbol QQQ --asset stock --start 2019-01-01 --end 2026-09-01
python reversal_backtest.py fetch --symbol BTC/USD --asset crypto --start 2021-01-01 --end 2026-09-01

python reversal_backtest.py run --symbol QQQ --asset stock --mode us_open --split-date 2024-01-01
python reversal_backtest.py run --symbol BTC/USD --asset crypto --mode us_open --split-date 2024-06-01
python reversal_backtest.py run --symbol BTC/USD --asset crypto --mode all_hours --split-date 2024-06-01

# a variant = a new trial
python reversal_backtest.py run --symbol QQQ --asset stock --split-date 2024-01-01 --params '{"target_r": 1.5}'

python test_reversal.py
```

If the SIP feed is refused on your data plan, use `--feed iex`. IEX prices are fine for this, though its volume is partial.

## Discipline

1. Tune only on dates before `--split-date`. Look at out-of-sample numbers once per variant.
2. Every `run` adds a trial row, including the ones you'd rather forget. The deflated Sharpe needs the full count.
3. Pass mark before AETHER paper mode: out-of-sample expectancy > 0 after costs, enough trades to mean something (aim for 100+), and a deflated Sharpe that clears your gate.
