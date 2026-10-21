"""AETHER Trend Gate: a read-only macro-trend filter (SPY vs its 200-day simple moving average).

Modules:
  config.py    settings from environment variables (paper/live data keys, window, timeouts)
  data.py      Alpaca daily-bar fetch with a hard timeout, plus verification (fail closed)
  strategy.py  the rule itself: pure, no I/O
  service.py   ties fetch -> verify -> evaluate together, with caching and logging
  api.py       FastAPI router (GET /signal/{symbol}) and a standalone app

Advisory only: nothing here places, changes or cancels orders.
"""
