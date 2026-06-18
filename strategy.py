"""
Trading Strategy Module
- Base strategy class
- MA Crossover
- RSI
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    market: str
    side: str       # "buy" | "sell" | "hold"
    confidence: float
    reason: str
    params: dict = field(default_factory=dict)


class BaseStrategy:
    def __init__(self, config: Dict):
        self.name = config.get("name", "base")
        self.params = config.get("params", {})

    def generate_signals(self, api, markets: List[str]) -> List[Signal]:
        raise NotImplementedError

    def get_candles(self, api, market: str, interval: str = "60", count: int = 100) -> List[Dict]:
        return api.get_candles(market, interval, count)

    @staticmethod
    def ma(prices: List[float], window: int) -> Optional[float]:
        if len(prices) < window:
            return None
        return sum(prices[-window:]) / window


class MACrossoverStrategy(BaseStrategy):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.short_window = self.params.get("short_window", 10)
        self.long_window = self.params.get("long_window", 30)
        self.interval = self.params.get("interval", "60")

    def generate_signals(self, api, markets: List[str]) -> List[Signal]:
        signals = []
        for market in markets:
            try:
                candles = self.get_candles(api, market, self.interval, self.long_window + 5)
                if len(candles) < self.long_window:
                    continue
                prices = [c["trade_price"] for c in candles]

                short_ma = self.ma(prices, self.short_window)
                long_ma = self.ma(prices, self.long_window)
                if short_ma is None or long_ma is None:
                    continue

                prev_short = self.ma(prices[:-1], self.short_window)
                prev_long = self.ma(prices[:-1], self.long_window)
                if prev_short is None or prev_long is None:
                    continue

                # Golden cross
                if prev_short <= prev_long and short_ma > long_ma:
                    conf = min(1.0, (short_ma - long_ma) / long_ma * 100)
                    signals.append(Signal(
                        market=market, side="buy", confidence=conf,
                        reason=f"Golden Cross MA{self.short_window}>{self.long_window}",
                        params={"short_ma": short_ma, "long_ma": long_ma},
                    ))
                # Death cross
                elif prev_short >= prev_long and short_ma < long_ma:
                    conf = min(1.0, (long_ma - short_ma) / long_ma * 100)
                    signals.append(Signal(
                        market=market, side="sell", confidence=conf,
                        reason=f"Death Cross MA{self.short_window}<{self.long_window}",
                        params={"short_ma": short_ma, "long_ma": long_ma},
                    ))
            except Exception as e:
                logger.error("Signal error %s: %s", market, e)
        return signals


class RSIStrategy(BaseStrategy):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.period = self.params.get("period", 14)
        self.overbought = self.params.get("overbought", 70)
        self.oversold = self.params.get("oversold", 30)
        self.interval = self.params.get("interval", "60")

    def rsi(self, prices: List[float]) -> Optional[float]:
        if len(prices) < self.period + 1:
            return None
        gains, losses = 0.0, 0.0
        for i in range(len(prices) - self.period, len(prices)):
            ch = prices[i] - prices[i - 1]
            if ch > 0:
                gains += ch
            else:
                losses += abs(ch)
        avg_gain = gains / self.period
        avg_loss = losses / self.period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def generate_signals(self, api, markets: List[str]) -> List[Signal]:
        signals = []
        for market in markets:
            try:
                candles = self.get_candles(api, market, self.interval, self.period + 5)
                if len(candles) < self.period:
                    continue
                prices = [c["trade_price"] for c in candles]
                rsi_val = self.rsi(prices)
                if rsi_val is None:
                    continue

                if rsi_val < self.oversold:
                    signals.append(Signal(
                        market=market, side="buy",
                        confidence=(self.oversold - rsi_val) / self.oversold,
                        reason=f"RSI Oversold {rsi_val:.1f}<{self.oversold}",
                        params={"rsi": rsi_val},
                    ))
                elif rsi_val > self.overbought:
                    signals.append(Signal(
                        market=market, side="sell",
                        confidence=(rsi_val - self.overbought) / (100 - self.overbought),
                        reason=f"RSI Overbought {rsi_val:.1f}>{self.overbought}",
                        params={"rsi": rsi_val},
                    ))
            except Exception as e:
                logger.error("RSI signal error %s: %s", market, e)
        return signals


def get_strategy(config: Dict) -> BaseStrategy:
    name = config.get("name", "ma_crossover")
    if name == "ma_crossover":
        return MACrossoverStrategy(config)
    elif name == "rsi":
        return RSIStrategy(config)
    raise ValueError(f"Unknown strategy: {name}")
