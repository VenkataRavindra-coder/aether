import risk_controller
import os
import logging
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

logger = logging.getLogger("AETHER_ALPACA")


def status_value(status) -> str:
    """alpaca-py statuses are enums: str(OrderStatus.FILLED) is 'OrderStatus.FILLED', not 'filled'."""
    return str(getattr(status, "value", status)).lower()


class AlpacaExecutionClient:
    def __init__(self, paper: bool = True, key_env: str = "APCA_API_KEY_ID",
                 secret_env: str = "APCA_API_SECRET_KEY"):
        self.paper = paper
        self.api_key = os.getenv(key_env)
        self.api_secret = os.getenv(secret_env)
        self.live_trading = os.getenv("LIVE_TRADING", "false").lower() == "true"
        self.paper = paper

        if not self.api_key or not self.api_secret:
            logger.warning("Alpaca API credentials missing. Running in simulation/stub mode.")
            self.client = None
        else:
            try:
                self.client = TradingClient(self.api_key, self.api_secret, paper=paper)
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
            return {
                "status": "success",
                "order_id": str(order.id),
                "state": status_value(order.status),
                "filled": status_value(order.status) == "filled",
                "qty": effective_qty,
            }

        except Exception as e:
            logger.error(f"Failed to submit live order to Alpaca: {str(e)}")
            return {"status": "error", "message": str(e), "qty": 0.0}

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

    def submit_market_order(self, symbol: str, side: str, qty: float = None, notional: float = None):
        """Fractional-capable DAY market order for stocks/ETFs (paper account only: client is paper=True)."""
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
            order = self.client.submit_order(order_data=MarketOrderRequest(**kwargs))
            logger.info(f"ETF ORDER PLACED: {order.id} | {side.upper()} {symbol} | {status_value(order.status)}")
            return {"status": "submitted", "order_id": str(order.id)}
        except Exception as e:
            logger.error(f"Failed to submit market order for {symbol}: {str(e)}")
            return {"status": "error", "message": str(e)}

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
