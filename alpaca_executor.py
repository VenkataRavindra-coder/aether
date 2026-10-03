import risk_controller
import os
import logging
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

logger = logging.getLogger("AETHER_ALPACA")

# alpaca-py sets no timeout on any HTTP call it makes (confirmed: TradingClient/RESTClient take
# no timeout parameter at all). A single hung request to Alpaca -- a dropped connection with no
# RST, a stalled TLS handshake -- blocks whichever lock-holding cycle made it forever, with
# nothing to recover it. This mounts a default timeout onto the underlying requests.Session,
# which alpaca-py does expose (as `_session`) even though it isn't part of its public API.
ALPACA_HTTP_TIMEOUT_SECONDS = float(os.getenv("ALPACA_HTTP_TIMEOUT_SECONDS", "15"))


class _TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, *args, timeout=ALPACA_HTTP_TIMEOUT_SECONDS, **kwargs):
        self._timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout
        return super().send(request, **kwargs)


def status_value(status) -> str:
    """alpaca-py statuses are enums: str(OrderStatus.FILLED) is 'OrderStatus.FILLED', not 'filled'."""
    return str(getattr(status, "value", status)).lower()


# Consecutive-failure threshold before a client declares itself unhealthy (see consecutive_errors
# below). Deliberately small: a broker that is genuinely down should stop new entries quickly,
# not after dozens of silently-swallowed retries.
BROKER_ERROR_FREEZE_THRESHOLD = 3


def classify_broker_error(e: Exception) -> str:
    """Buckets a broker-call failure so callers can react differently instead of treating a
    rate limit, a timeout and a flat-out rejection identically (the previous behaviour: every
    failure collapsed into the same generic {"status": "error"}, logged, and otherwise ignored
    -- nothing backed off on a 429, and nothing ever stopped trying after N straight failures)."""
    import requests
    if isinstance(e, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "connection"
    code = getattr(e, "status_code", None)
    if code == 429:
        return "rate_limited"
    if isinstance(code, int) and 500 <= code < 600:
        return "server_error"
    if isinstance(code, int) and 400 <= code < 500:
        return "client_error"
    return "unknown"


class AlpacaExecutionClient:
    def __init__(self, paper: bool = True, key_env: str = "APCA_API_KEY_ID",
                 secret_env: str = "APCA_API_SECRET_KEY"):
        self.paper = paper
        self.api_key = os.getenv(key_env)
        self.api_secret = os.getenv(secret_env)
        self.live_trading = os.getenv("LIVE_TRADING", "false").lower() == "true"
        self.paper = paper
        # Consecutive broker-call failures since the last success. A caller can check this after
        # a failed order and freeze new entries once it crosses BROKER_ERROR_FREEZE_THRESHOLD,
        # instead of silently retrying forever against a broker that is genuinely down.
        self.consecutive_errors = 0

        if not self.api_key or not self.api_secret:
            logger.warning("Alpaca API credentials missing. Running in simulation/stub mode.")
            self.client = None
        else:
            try:
                self.client = TradingClient(self.api_key, self.api_secret, paper=paper)
                adapter = _TimeoutHTTPAdapter()
                self.client._session.mount("https://", adapter)
                self.client._session.mount("http://", adapter)
                logger.info(f"Alpaca Trading Client initialized ({'PAPER' if paper else 'LIVE MONEY'} account). "
                            f"LIVE_TRADING flag: {self.live_trading}")
            except Exception as e:
                logger.error(f"Failed to initialize Alpaca Trading Client: {str(e)}")
                self.client = None

    def get_position_qty(self, symbol: str):
        """Returns the REAL long quantity held at the broker for `symbol`, or None if unknown
        (no credentials configured, or the lookup failed). Callers should fall back to locally
        tracked state only when this returns None — the broker is the source of truth whenever
        it is reachable, because the naked-short guard must not rely solely on a local file that
        can drift after a crash, a manual trade, or a partial fill."""
        if not self.client:
            return None
        try:
            positions = self.client.get_all_positions()
            target = symbol.replace("/", "").upper()
            for p in positions:
                if p.symbol.replace("/", "").upper() == target:
                    return float(p.qty)
            return 0.0
        except Exception as e:
            logger.error(f"Failed to fetch live position for {symbol}: {str(e)}")
            return None

    def execute_order(self, symbol: str, qty: float, side: str, limit_price: float,
                       is_closing: bool = False, available_long: float = 0.0):
        """Dispatches orders to Alpaca with position guards to prevent naked shorts on
        crypto assets where Alpaca does not support short selling. Returns the ACTUAL
        quantity sent to the broker as `qty` in the response — callers must book state
        changes against that value, not the originally requested quantity, since a
        closing sell may be capped below the requested size."""
        side_upper = side.upper()

        if side_upper == "SELL" and not is_closing and available_long <= 0:
            logger.warning(
                f"Blocked naked SELL for {symbol}: no long inventory to close, "
                f"and shorting is restricted/unsupported for this asset."
            )
            return {"status": "rejected", "reason": "no_position_to_close", "filled": False, "qty": 0.0}

        effective_qty = min(qty, available_long) if (side_upper == "SELL" and is_closing) else qty
        if effective_qty <= 0:
            return {"status": "rejected", "reason": "zero_quantity", "filled": False, "qty": 0.0}

        allowed, risk_reason = risk_controller.check(
            "crypto", symbol, side_upper, effective_qty * float(limit_price), is_closing,
            real_money=(not self.paper) and self.live_trading)
        if not allowed:
            return {"status": "rejected", "reason": f"risk:{risk_reason}", "filled": False, "qty": 0.0}

        if not self.live_trading:
            logger.info(
                f"[SHADOW RECORDER MODE] Captured {side_upper} signal for "
                f"{effective_qty} {symbol} at ${limit_price} (execution suppressed)."
            )
            return {"status": "shadow_recorded", "order_id": "shadow_sim_000", "filled": False, "qty": effective_qty}

        if not self.client:
            logger.error("Live trading is enabled, but the Alpaca client is not initialized.")
            return {"status": "error", "message": "Alpaca client uninitialized.", "qty": 0.0}

        try:
            order_side = OrderSide.BUY if side_upper == "BUY" else OrderSide.SELL

            request_params = LimitOrderRequest(
                symbol=symbol,
                qty=effective_qty,
                side=order_side,
                time_in_force=TimeInForce.GTC,
                limit_price=limit_price,
            )

            order = self.client.submit_order(order_data=request_params)
            logger.info(f"LIVE ORDER PLACED: {order.id} | Side: {side_upper} | Status: {order.status}")
            self.consecutive_errors = 0
            return {
                "status": "success",
                "order_id": str(order.id),
                "state": status_value(order.status),
                "filled": status_value(order.status) == "filled",
                "qty": effective_qty,
            }

        except Exception as e:
            error_class = classify_broker_error(e)
            self.consecutive_errors += 1
            logger.error(f"Failed to submit live order to Alpaca: {str(e)} (class={error_class}, "
                        f"consecutive_errors={self.consecutive_errors})")
            if error_class == "rate_limited":
                import time as _time
                _time.sleep(2)
            return {"status": "error", "message": str(e), "qty": 0.0, "error_class": error_class,
                    "consecutive_errors": self.consecutive_errors}

    def get_open_positions(self):
        """Fetches active open positions for portfolio residual tracking."""
        if not self.client:
            return []
        try:
            return self.client.get_all_positions()
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {str(e)}")
            return []

    def get_account_balance(self):
        """Fetches live paper buying power, cash, and portfolio value from Alpaca.

        Returns a dict with an explicit `source` field so callers (and the dashboard)
        can tell real broker data apart from a stub. Never fabricate numbers on an
        API failure — that would make a broken credential or network outage look
        like a healthy $10k/$20k account."""
        if not self.client:
            return {"cash": "0.00", "buying_power": "0.00", "portfolio_value": "0.00", "source": "no_credentials"}
        try:
            account = self.client.get_account()
            return {
                "cash": str(account.cash),
                "buying_power": str(account.buying_power),
                "portfolio_value": str(account.portfolio_value),
                "source": "live",
            }
        except Exception as e:
            logger.error(f"Failed to fetch live account balance: {str(e)}")
            return {"cash": None, "buying_power": None, "portfolio_value": None, "source": "error"}

    def get_clock(self):
        """Alpaca market clock (knows holidays), or None if unavailable."""
        if not self.client:
            return None
        try:
            return self.client.get_clock()
        except Exception as e:
            logger.error(f"Failed to fetch market clock: {str(e)}")
            return None

    def get_positions_map(self):
        """{symbol: quantity} of all broker positions, or None if the lookup failed."""
        if not self.client:
            return None
        try:
            return {p.symbol: float(p.qty) for p in self.client.get_all_positions()}
        except Exception as e:
            logger.error(f"Failed to fetch positions: {str(e)}")
            return None

    def open_order_symbols(self):
        """Symbols that currently have open orders, or None if the lookup failed."""
        if not self.client:
            return None
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            return {o.symbol for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))}
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {str(e)}")
            return None

    def submit_market_order(self, symbol: str, side: str, qty: float = None, notional: float = None,
                             client_order_id: str = None):
        """Fractional-capable DAY market order for stocks/ETFs (paper account only: client is paper=True).

        client_order_id makes a retry idempotent. If we submit, the request reaches Alpaca, but the
        response is lost (timeout, reset, crash before we read it), a naive retry would place a
        SECOND order. With client_order_id set, a submit that errors out is followed by a lookup of
        that same id: if the first attempt actually landed, we return its order instead of trying
        again and doubling the position."""
        side_upper = side.upper()
        # US engine is long-only: SELLs only reduce holdings, so they count as closing.
        allowed, risk_reason = risk_controller.check(
            "us", symbol, side_upper, notional, side_upper == "SELL", real_money=not self.paper)
        if not allowed:
            return {"status": "error", "message": f"risk:{risk_reason}"}
        if not self.client:
            return {"status": "error", "message": "Alpaca client uninitialized."}
        try:
            from alpaca.trading.requests import MarketOrderRequest
            kwargs = {"symbol": symbol,
                      "side": OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL,
                      "time_in_force": TimeInForce.DAY}
            if qty is not None:
                kwargs["qty"] = qty
            else:
                kwargs["notional"] = round(float(notional), 2)
            if client_order_id:
                kwargs["client_order_id"] = client_order_id
            order = self.client.submit_order(order_data=MarketOrderRequest(**kwargs))
            logger.info(f"ETF ORDER PLACED: {order.id} | {side.upper()} {symbol} | {status_value(order.status)}")
            self.consecutive_errors = 0
            return {"status": "submitted", "order_id": str(order.id)}
        except Exception as e:
            if client_order_id:
                try:
                    existing = self.client.get_order_by_client_id(client_order_id)
                    logger.warning(f"Submit for {symbol} errored ({e}); an order already exists under "
                                   f"client_order_id={client_order_id} (id {existing.id}) -- treating it "
                                   f"as already submitted instead of retrying.")
                    self.consecutive_errors = 0
                    return {"status": "submitted", "order_id": str(existing.id), "recovered": True}
                except Exception:
                    pass
            error_class = classify_broker_error(e)
            self.consecutive_errors += 1
            logger.error(f"Failed to submit market order for {symbol}: {str(e)} (class={error_class}, "
                        f"consecutive_errors={self.consecutive_errors})")
            if error_class == "rate_limited":
                import time as _time
                _time.sleep(2)
            return {"status": "error", "message": str(e), "error_class": error_class,
                    "consecutive_errors": self.consecutive_errors}

    def cancel_order(self, order_id: str) -> bool:
        """Cancels an open order. False if it could not be cancelled (e.g. it already filled)."""
        if not self.client:
            return False
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception as e:
            logger.warning(f"Cancel failed for {order_id}: {str(e)}")
            return False

    def account_id(self):
        """Broker account number (cached). None if unavailable."""
        if getattr(self, "_account_id", None):
            return self._account_id
        if not self.client:
            return None
        try:
            self._account_id = str(self.client.get_account().account_number)
            return self._account_id
        except Exception as e:
            logger.warning(f"Could not read account id: {str(e)}")
            return None

    def wait_for_fill(self, order_id: str, timeout: float = 90.0, poll: float = 2.0):
        """Polls until the order reaches a final state (or times out). Returns
        {'state', 'filled_qty', 'avg_price'} using the broker's own figures."""
        import time as _time
        final = {"filled", "canceled", "expired", "rejected", "done_for_day"}
        deadline = _time.time() + timeout
        last = {"state": "unknown", "filled_qty": 0.0, "avg_price": None}
        while True:
            try:
                o = self.client.get_order_by_id(order_id)
                last = {"state": status_value(o.status),
                        "filled_qty": float(o.filled_qty or 0.0),
                        "avg_price": float(o.filled_avg_price) if o.filled_avg_price else None}
                if last["state"] in final:
                    return last
            except Exception as e:
                logger.warning(f"Order poll failed: {str(e)}")
            if _time.time() >= deadline:
                return last
            _time.sleep(poll)
