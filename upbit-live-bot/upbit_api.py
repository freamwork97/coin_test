"""
Upbit API Wrapper - Read Only + Paper Trading Simulation
- Public API calls only (market list, ticker, candles, orderbook)
- NEVER calls real order API (requires explicit approval to enable live_trading)
"""

import requests
import time
import uuid
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class UpbitAPI:
    def __init__(self):
        self.base_url = "https://api.upbit.com/v1"
        self.session = requests.Session()

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed [{endpoint}]: {e}")
            return None

    def get_market_list(self) -> List[Dict]:
        return self._get("/market/all", {"isDetails": "true"}) or []

    def get_ticker(self, markets: List[str]) -> List[Dict]:
        market_str = ",".join(markets)
        return self._get("/ticker", {"markets": market_str}) or []

    def get_candles(self, market: str, interval: str = "60", count: int = 100) -> List[Dict]:
        endpoint = f"/candles/minutes/{interval}"
        return self._get(endpoint, {"market": market, "count": count}) or []

    def get_day_candles(self, market: str, count: int = 100) -> List[Dict]:
        return self._get("/candles/days", {"market": market, "count": count}) or []

    def get_orderbook(self, markets: List[str]) -> List[Dict]:
        market_str = ",".join(markets)
        return self._get("/orderbook", {"markets": market_str}) or []

    def simulate_order(self, market: str, side: str, volume: float,
                       price: float, fee_rate: float, slippage: float) -> Dict:
        """
        Simulate an order with fee + slippage. NO real API call.
        side = "bid" (buy) or "ask" (sell)
        """
        if side == "bid":
            fill_price = price * (1 + slippage)
        else:
            fill_price = price * (1 - slippage)

        order_value = volume * fill_price
        fee = order_value * fee_rate
        slippage_cost = abs(price - fill_price) * volume

        return {
            "uuid": str(uuid.uuid4()),
            "market": market,
            "side": side,
            "price": fill_price,
            "volume": volume,
            "fee": fee,
            "slippage_cost": slippage_cost,
            "total_value": order_value,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "simulated": True,
        }
