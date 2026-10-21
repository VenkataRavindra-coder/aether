# SPY macro trend signal API

GET /signal/SPY returns a read-only 200-session SPY trend filter. It never submits or changes orders. The existing AETHER session and Cloudflare Access middleware protects the route.

The service compares the latest completed, split- and dividend-adjusted daily close with the mean of the latest 200 completed SPY closes. A strict close > SMA(200) is RISK_ON (BUY for a new entry, HOLD_LONG if already long). A close equal to or below the SMA is DEFENSIVE_CASH (DEFENSIVE_CASH for a new entry, EXIT if already long). This is a macro filter only; it does not define position size, execution, stops, or whether a holding exists.

The response includes the signal date, close, SMA, selected account mode, market-data feed, adjustment policy, and whether the response came from the short cache. If data cannot be verified, the route returns HTTP 503 with signal=NO_SIGNAL and recommended_action=NO_ACTION; it never reuses an old signal after a failed refresh.

## Configuration

Paper mode is the default. Add these non-secret settings to .env:

    ALPACA_MODE=paper
    ALPACA_DATA_FEED=iex
    ALPACA_HTTP_TIMEOUT_SECONDS=8
    SPY_SMA_LOOKBACK_CALENDAR_DAYS=420
    MACRO_SIGNAL_CACHE_SECONDS=60

Paper mode uses APCA_API_KEY_ID and APCA_API_SECRET_KEY. Live data mode requires ALPACA_MODE=live and the separate APCA_LIVE_API_KEY_ID / APCA_LIVE_API_SECRET_KEY values. Keep the live values in the existing mode-600 .env.live, which is loaded only by aether.service. The endpoint remains read-only in either mode.

iex is the default feed. Change it to sip only when the Alpaca account is entitled to that feed. Do not put credentials in URLs, source files, or service command lines.

## Run and operate on the VM

The API is mounted in the existing AETHER FastAPI process. After merging and deploying the code through the normal release process:

    cd ~/ratelark
    source .venv/bin/activate
    python -m py_compile aether_engine.py macro_trend/*.py
    sudo systemctl restart aether
    sudo systemctl status aether --no-pager
    sudo journalctl -u aether -n 100 --no-pager

Test through the authenticated dashboard session or its SSH tunnel; the route is intentionally not public:

    curl -i http://127.0.0.1:8000/signal/SPY

A successful response includes signal, latest_close, sma_200, signal_date, action_on_new_entry, and action_if_long. A 503 means no current recommendation is available; treat it as no signal and do not trade from stale values. The 200-day comparison is evaluated only on completed market sessions, validated against Alpaca's clock and calendar. HTTP requests have explicit timeouts, and missing/duplicate expected session bars fail closed.
