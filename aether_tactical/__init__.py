"""AETHER tactical paper tests (pre-registered 2026-10-21, strategy.md "Tactical paper tests").

Two intraday SPY strategies, scored as PAPER trades only: no module here places, changes or cancels
an order. Each trading day after the close, every strategy is replayed bar by bar on that day's real
IEX 1-minute bars (decisions use only bars already closed), and the result is appended to a ledger.
After the pre-registered sample, the ledger gives a verdict before any money is involved.

  market_time.py       New York session open/close from Alpaca's calendar (holidays, early closes)
  bars.py              IEX 1-minute bars for one session, fetched with a timeout and verified
  orb_engine.py        Strategy A: 15-minute opening range breakout (pure)
  engulfing_engine.py  Strategy B: 15-minute bullish engulfing scalp (pure)
  sim.py               shared exit simulation and costs (pure)
  ledger.py            append-only JSONL ledger + summary/verdict
  run_day.py           CLI for the daily timer: score one session
  api_tactical.py      read-only endpoints: GET /tactical/signal/{symbol}, GET /tactical/ledger
"""
