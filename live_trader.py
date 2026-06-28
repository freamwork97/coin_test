"""
Live Trading Engine — Upbit Real Orders
========================================
- Uses LiveUpbitAPI for actual market buy/sell orders
- Tracks positions based on exchange account data
- Records trades to data/live_trades.jsonl
- fee: Upbit standard 0.05%
"""

import json
import os
import time
import logging
from typing import Dict, Optional
from datetime import datetime

from live_api import LiveUpbitAPI

logger = logging.getLogger(__name__)

BOT_DIR = os.path.expanduser("~/.openclaw/workspace/upbit-live-bot")
DATA_DIR = os.path.join(BOT_DIR, "data")
RUNTIME_DIR = os.path.join(BOT_DIR, "runtime")
TRADES_FILE = os.path.join(DATA_DIR, "live_trades.jsonl")
EQUITY_FILE = os.path.join(DATA_DIR, "live_equity_curve.csv")


class LiveTrader:
    def __init__(self, config: Dict):
        self.config = config
        self.api = LiveUpbitAPI()
        pt = config["live_trading"]
        self.initial_balance = pt.get("initial_balance_krw", 0)
        self.fee_rate = pt.get("fee_rate", 0.0005)
        self.position_size_pct = pt.get("position_size_pct", 0.10)
        self.cycle_seconds = pt.get("cycle_seconds", 60)

        self.trades: list = []
        self.total_fees = 0.0
        self.peak_value = 0.0
        self.consecutive_losses = 0
        self.daily_pnl: Dict[str, float] = {}
        self.initial_balance_recorded = False

        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        self._init_balance()

    def _init_balance(self):
        """Record initial balance from exchange."""
        krw = self.api.get_krw_balance()
        if self.initial_balance == 0:
            self.initial_balance = krw
        self.peak_value = krw
        logger.info("LiveTrader initialized: KRW balance=₩%.0f", krw)

    def get_krw_balance(self) -> float:
        return self.api.get_krw_balance()

    def get_positions(self) -> Dict[str, float]:
        """Returns {market: volume} from exchange."""
        return self.api.get_positions_from_exchange()

    def get_portfolio_value(self) -> float:
        """KRW balance + value of all coin positions."""
        krw = self.get_krw_balance()
        positions = self.get_positions()
        if positions:
            markets = list(positions.keys())
            tickers = self.api.get_ticker(markets)
            for tk in tickers:
                m = tk["market"]
                if m in positions:
                    krw += positions[m] * tk["trade_price"]
        return krw

    def get_unrealized_pnl(self) -> Dict[str, float]:
        """Unrealized PnL per position."""
        pnl = {}
        positions = self.get_positions()
        if not positions:
            return pnl
        markets = list(positions.keys())
        tickers = self.api.get_ticker(markets)
        price_map = {tk["market"]: tk["trade_price"] for tk in tickers}
        for m, vol in positions.items():
            avg = self.api.get_average_buy_price(m)
            if avg and m in price_map:
                pnl[m] = (price_map[m] - avg) * vol
        return pnl

    def buy(self, market: str, amount_krw: float) -> Optional[Dict]:
        """Execute real market buy. Returns order dict or None."""
        krw = self.get_krw_balance()
        if amount_krw > krw:
            logger.error("Insufficient KRW: %.0f < %.0f", krw, amount_krw)
            return None
        if amount_krw < 5000:
            logger.warning("Amount too small: ₩%.0f", amount_krw)
            return None

        order = self.api.market_buy(market, amount_krw)
        if not order:
            logger.error("Market buy failed for %s", market)
            return None

        # Retrieve actual execution details (POST response lacks executed_volume/paid_fee)
        uuid = order.get("uuid")
        executed_volume = "0"
        paid_fee = "0"
        if uuid:
            time.sleep(0.3)  # brief wait for order to settle
            detail = self.api.get_order(uuid)
            if detail:
                executed_volume = str(detail.get("executed_volume", "0") or "0")
                paid_fee = str(detail.get("paid_fee", "0") or "0")

        # Record trade
        trade = {
            "uuid": uuid,
            "market": market,
            "side": "buy",
            "amount_krw": amount_krw,
            "volume_executed": executed_volume,
            "fee": paid_fee,
            "timestamp": order.get("created_at") or datetime.now().isoformat(),
        }
        self._record_trade(trade)
        logger.info("BUY  %s  ₩%.0f  order=%s", market, amount_krw, trade["uuid"])
        return trade

    def sell(self, market: str, volume: Optional[float] = None) -> Optional[Dict]:
        """Execute real market sell. volume=None → sell entire position."""
        positions = self.get_positions()
        if market not in positions:
            logger.warning("No position for %s", market)
            return None

        if volume is None:
            volume = positions[market]
        if volume > positions[market]:
            logger.warning("Volume exceeds position: %.6f > %.6f", volume, positions[market])
            volume = positions[market]

        order = self.api.market_sell(market, volume)
        if not order:
            logger.error("Market sell failed for %s", market)
            return None

        # Retrieve actual execution details (POST response lacks executed_volume/paid_fee)
        uuid = order.get("uuid")
        executed_volume = "0"
        paid_fee = "0"
        executed_funds = "0"
        detail = None
        if uuid:
            time.sleep(0.3)  # brief wait for order to settle
            detail = self.api.get_order(uuid)
            if detail:
                executed_volume = str(detail.get("executed_volume", "0") or "0")
                paid_fee = str(detail.get("paid_fee", "0") or "0")
                executed_funds = str(detail.get("executed_funds", "0") or "0")

        trade = {
            "uuid": uuid,
            "market": market,
            "side": "sell",
            "volume": volume,
            "volume_executed": executed_volume,
            "executed_funds": executed_funds,
            "fee": paid_fee,
            "timestamp": order.get("created_at") or datetime.now().isoformat(),
        }
        self._record_trade(trade)

        # Track consecutive losses (approximate — check PnL later)
        # We'll refine based on order result
        logger.info("SELL %s  vol=%.6f  order=%s", market, volume, trade["uuid"])
        return trade

    def _record_trade(self, trade: Dict):
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(trade, ensure_ascii=False) + "\n")
        self.trades.append(trade)
        self._snapshot_equity()

    def _snapshot_equity(self):
        eq = self.get_portfolio_value()
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        if not os.path.exists(EQUITY_FILE):
            with open(EQUITY_FILE, "w") as f:
                f.write("timestamp,equity\n")
        with open(EQUITY_FILE, "a") as f:
            f.write(f"{ts},{eq:.2f}\n")

    # ------------------------------------------------------------------
    # Status for monitoring
    # ------------------------------------------------------------------
    def get_status(self) -> Dict:
        """Get full status with minimal API calls — accounts once, ticker once."""
        accounts = self.api.get_accounts()
        positions = self.api.get_positions_from_exchange()

        # Extract KRW from accounts
        krw = 0.0
        for acc in accounts:
            if acc["currency"] == "KRW":
                krw = float(acc["balance"])
                break

        # Get ticker once for all positions
        ticker_map: Dict[str, float] = {}
        if positions:
            tickers = self.api.get_ticker(list(positions.keys()))
            ticker_map = {tk["market"]: tk["trade_price"] for tk in tickers}

        # Portfolio value
        pv = krw
        positions_value = 0.0
        for m, vol in positions.items():
            price = ticker_map.get(m, 0.0)
            pv += vol * price
            positions_value += vol * price

        # Unrealized PnL (from accounts avg_buy_price, no extra API call)
        unreal: Dict[str, float] = {}
        for m, vol in positions.items():
            currency = m.replace("KRW-", "")
            avg_price = 0.0
            for acc in accounts:
                if acc["currency"] == currency:
                    avg = acc.get("avg_buy_price", "0")
                    if avg and float(avg) > 0:
                        avg_price = float(avg)
                    break
            if avg_price > 0 and m in ticker_map:
                unreal[m] = (ticker_map[m] - avg_price) * vol

        return {
            "timestamp": datetime.now().isoformat(),
            "krw_balance": krw,
            "positions_count": len(positions),
            "positions": {
                m: {
                    "volume": v,
                    "unrealized_pnl": unreal.get(m, 0.0),
                }
                for m, v in positions.items()
            },
            "portfolio_value": pv,
            "positions_value": positions_value,
            "total_return_pct": ((pv - self.initial_balance) / self.initial_balance * 100) if self.initial_balance > 0 else 0,
            "total_trades": len(self.trades),
        }
