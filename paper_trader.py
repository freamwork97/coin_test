"""
Paper Trading Engine
- Virtual balance / positions
- Fee 0.05% + slippage 0.03% applied on every fill
- Records trades to data/trades.jsonl
- Records equity snapshots to data/equity_curve.csv
"""

import json
import os
import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# Paths relative to bot directory
BOT_DIR = os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot")
DATA_DIR = os.path.join(BOT_DIR, "data")
RUNTIME_DIR = os.path.join(BOT_DIR, "runtime")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.jsonl")
EQUITY_FILE = os.path.join(DATA_DIR, "equity_curve.csv")


class PaperTrader:
    def __init__(self, config: Dict, api):
        self.config = config
        self.api = api
        pt = config["paper_trading"]
        self.initial_balance = pt["initial_balance_krw"]
        self.balance = self.initial_balance
        self.fee_rate = pt["fee_rate"]
        self.slippage = pt["slippage"]
        self.position_size_pct = pt.get("position_size_pct", 0.10)
        self.cycle_seconds = pt.get("cycle_seconds", 60)

        self.positions: Dict[str, dict] = {}   # market -> {volume, avg_buy_price, total_cost}
        self.trades: List[dict] = []
        self.daily_pnl: Dict[str, float] = {}
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self.peak_value = self.initial_balance
        self.consecutive_losses = 0

        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE) as f:
                s = json.load(f)
            self.balance = s.get("balance", self.initial_balance)
            self.total_fees = s.get("total_fees", 0.0)
            self.total_slippage = s.get("total_slippage", 0.0)
            self.peak_value = s.get("peak_value", self.initial_balance)
            self.consecutive_losses = s.get("consecutive_losses", 0)
            self.daily_pnl = s.get("daily_pnl", {})
            self.positions = s.get("positions", {})

            # Load trades from jsonl
            if os.path.exists(TRADES_FILE):
                with open(TRADES_FILE) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.trades.append(json.loads(line))

            logger.info("Loaded saved state (balance=%.0f, trades=%d)", self.balance, len(self.trades))
        except Exception as e:
            logger.error("Failed to load state: %s", e)

    def _save_state(self):
        s = {
            "balance": self.balance,
            "total_fees": self.total_fees,
            "total_slippage": self.total_slippage,
            "peak_value": self.peak_value,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": self.daily_pnl,
            "positions": self.positions,
            "updated_at": datetime.now().isoformat(),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2)

    def _append_trade(self, trade: dict):
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(trade, ensure_ascii=False) + "\n")
        self.trades.append(trade)

    def _append_equity(self, ts: str, equity: float):
        """Append a row to equity_curve.csv; create header if new."""
        if not os.path.exists(EQUITY_FILE):
            with open(EQUITY_FILE, "w") as f:
                f.write("timestamp,equity\n")
        with open(EQUITY_FILE, "a") as f:
            f.write(f"{ts},{equity:.2f}\n")

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------
    def buy(self, market: str, volume: Optional[float] = None,
            price: Optional[float] = None,
            amount_krw: Optional[float] = None) -> Optional[dict]:
        """Execute paper buy. Returns trade dict or None."""
        if price is None:
            t = self.api.get_ticker([market])
            if not t:
                return None
            price = t[0]["trade_price"]

        if amount_krw is not None:
            volume = amount_krw / price

        if volume is None or volume <= 0:
            logger.error("Invalid volume for %s", market)
            return None

        order_value = volume * price
        fee = order_value * self.fee_rate
        total_cost = order_value + fee

        if total_cost > self.balance:
            logger.warning("Insufficient balance: %.0f < %.0f", self.balance, total_cost)
            return None

        order = self.api.simulate_order(market, "bid", volume, price, self.fee_rate, self.slippage)
        fill_price = order["price"]

        self.balance -= order_value + fee
        self.total_fees += fee
        self.total_slippage += order["slippage_cost"]

        # Update position
        if market in self.positions:
            pos = self.positions[market]
            new_cost = pos["total_cost"] + order_value
            new_vol = pos["volume"] + volume
            pos["avg_buy_price"] = new_cost / new_vol
            pos["volume"] = new_vol
            pos["total_cost"] = new_cost
        else:
            self.positions[market] = {
                "volume": volume,
                "avg_buy_price": fill_price,
                "total_cost": order_value,
            }

        trade = {
            "uuid": order["uuid"],
            "market": market,
            "side": "buy",
            "price": fill_price,
            "volume": volume,
            "fee": fee,
            "slippage_cost": order["slippage_cost"],
            "total_value": order_value,
            "balance_after": self.balance,
            "timestamp": order["created_at"],
        }
        self._append_trade(trade)
        self._save_state()
        self._snapshot_equity()
        logger.info("BUY  %s  vol=%.6f  price=%.0f  fee=%.2f", market, volume, fill_price, fee)
        return trade

    def sell(self, market: str, volume: Optional[float] = None,
             price: Optional[float] = None) -> Optional[dict]:
        """Execute paper sell. volume=None → sell entire position."""
        if market not in self.positions:
            logger.warning("No position for %s", market)
            return None

        pos = self.positions[market]
        if volume is None:
            volume = pos["volume"]
        if volume > pos["volume"]:
            logger.warning("Not enough volume for %s: %.6f < %.6f", market, pos["volume"], volume)
            return None

        if price is None:
            t = self.api.get_ticker([market])
            if not t:
                return None
            price = t[0]["trade_price"]

        order = self.api.simulate_order(market, "ask", volume, price, self.fee_rate, self.slippage)
        fill_price = order["price"]

        sell_value = volume * fill_price
        fee = sell_value * self.fee_rate
        net_value = sell_value - fee
        cost_basis = volume * pos["avg_buy_price"]
        realized_pnl = net_value - cost_basis

        self.balance += net_value
        self.total_fees += fee
        self.total_slippage += order["slippage_cost"]

        # Update consecutive loss tracker
        if realized_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        # Update position
        remaining = pos["volume"] - volume
        if remaining > 1e-12:
            pos["volume"] = remaining
            pos["total_cost"] = remaining * pos["avg_buy_price"]
        else:
            del self.positions[market]

        trade = {
            "uuid": order["uuid"],
            "market": market,
            "side": "sell",
            "price": fill_price,
            "volume": volume,
            "fee": fee,
            "slippage_cost": order["slippage_cost"],
            "total_value": sell_value,
            "realized_pnl": realized_pnl,
            "balance_after": self.balance,
            "timestamp": order["created_at"],
        }
        self._append_trade(trade)
        self._save_state()

        # Daily PnL
        today = datetime.now().strftime("%Y-%m-%d")
        self.daily_pnl[today] = self.daily_pnl.get(today, 0.0) + realized_pnl

        self._snapshot_equity()
        logger.info("SELL %s  vol=%.6f  price=%.0f  pnl=%.2f", market, volume, fill_price, realized_pnl)
        return trade

    def _snapshot_equity(self):
        eq = self.get_portfolio_value()
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        self._append_equity(ts, eq)

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    def get_portfolio_value(self) -> float:
        value = self.balance
        if self.positions:
            markets = list(self.positions.keys())
            tickers = self.api.get_ticker(markets)
            for tk in tickers:
                m = tk["market"]
                if m in self.positions:
                    value += self.positions[m]["volume"] * tk["trade_price"]
        return value

    def get_unrealized_pnl(self) -> Dict[str, float]:
        pnl = {}
        if self.positions:
            markets = list(self.positions.keys())
            tickers = self.api.get_ticker(markets)
            for tk in tickers:
                m = tk["market"]
                if m in self.positions:
                    pos = self.positions[m]
                    pnl[m] = (tk["trade_price"] - pos["avg_buy_price"]) * pos["volume"]
        return pnl

    def get_positions_value(self) -> float:
        pv = 0.0
        if self.positions:
            markets = list(self.positions.keys())
            tickers = self.api.get_ticker(markets)
            for tk in tickers:
                m = tk["market"]
                if m in self.positions:
                    pv += self.positions[m]["volume"] * tk["trade_price"]
        return pv
