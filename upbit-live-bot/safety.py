"""
Safety / Risk Management
- Enforces daily loss limit, MDD, single-coin weight, consecutive-loss stop
- live_trading_enabled must be false (no real order API calls)
"""

import logging
from typing import Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class SafetyManager:
    def __init__(self, config: Dict):
        s = config["safety"]
        self.max_daily_loss_rate = s["max_daily_loss_rate"]      # e.g. -0.05
        self.max_mdd_rate = s["max_mdd_rate"]                    # e.g. -0.10
        self.max_single_coin_weight = s["max_single_coin_weight"]  # e.g. 0.30
        self.consecutive_loss_stop = s["consecutive_loss_stop"]    # e.g. 5
        self.emergency_stop = s.get("emergency_stop", False)
        self.live_trading_enabled = s.get("live_trading_enabled", False)

    def check(self, trader, portfolio_value: float) -> dict:
        """
        Run all safety checks.
        Returns {"can_trade": bool, "violations": [...], "details": {...}}
        """
        violations = []

        # 1. Live trading must be disabled
        if self.live_trading_enabled:
            violations.append("live_trading_enabled is TRUE — abort (safety policy)")

        # 2. Emergency stop
        if self.emergency_stop:
            violations.append("EMERGENCY STOP active")

        # 3. Daily loss
        today = datetime.now().strftime("%Y-%m-%d")
        daily_pnl = trader.daily_pnl.get(today, 0.0)
        daily_loss_rate = daily_pnl / trader.initial_balance
        if daily_loss_rate < self.max_daily_loss_rate:
            violations.append(
                f"Daily loss {daily_loss_rate:.2%} exceeds limit {self.max_daily_loss_rate:.2%}"
            )

        # 4. MDD
        peak = trader.peak_value
        if portfolio_value > peak:
            trader.peak_value = portfolio_value
            peak = portfolio_value
        mdd = (peak - portfolio_value) / peak if peak > 0 else 0
        if mdd > abs(self.max_mdd_rate):
            violations.append(f"MDD {mdd:.2%} exceeds limit {abs(self.max_mdd_rate):.2%}")

        # 5. Consecutive losses
        if trader.consecutive_losses >= self.consecutive_loss_stop:
            violations.append(
                f"Consecutive losses {trader.consecutive_losses} >= stop {self.consecutive_loss_stop}"
            )

        # 6. Single-coin weight
        if trader.positions:
            tickers = trader.api.get_ticker(list(trader.positions.keys()))
            price_map = {tk["market"]: tk["trade_price"] for tk in tickers}
            for m, pos in trader.positions.items():
                cp = price_map.get(m, 0)
                w = (pos["volume"] * cp) / portfolio_value if portfolio_value > 0 else 0
                if w > self.max_single_coin_weight:
                    violations.append(f"Weight {m} = {w:.2%} > limit {self.max_single_coin_weight:.2%}")

        can = len(violations) == 0
        if not can:
            logger.warning("Safety violations: %s", violations)

        return {
            "can_trade": can,
            "violations": violations,
            "daily_loss_rate": daily_loss_rate,
            "mdd": mdd,
            "consecutive_losses": trader.consecutive_losses,
        }

    def check_single_coin_weight(self, trader, market: str, additional_value: float,
                                  portfolio_value: float) -> bool:
        current_val = 0.0
        if market in trader.positions:
            t = trader.api.get_ticker([market])
            cp = t[0]["trade_price"] if t else 0
            current_val = trader.positions[market]["volume"] * cp
        new_weight = (current_val + additional_value) / portfolio_value if portfolio_value > 0 else 0
        return new_weight <= self.max_single_coin_weight
