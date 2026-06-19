"""
Upbit Live Trading Bot — Main Event Loop
=========================================
- Real market orders via JWT-authenticated API
- 1-min cycles: safety → signals → execute → equity snapshot
- Strategy: MA Crossover (inherited from paper bot)
- Enhanced safety: tighter limits, max positions, cooldown after loss
- Writes runtime/status.json, data/live_trades.jsonl, data/live_equity_curve.csv
"""

import json
import os
import sys
import time
import logging
import signal
from datetime import datetime
from typing import Dict, Optional, Set

BOT_DIR = os.path.expanduser("~/.openclaw/workspace/upbit-live-bot")
LOG_DIR = os.path.join(BOT_DIR, "logs")
RUNTIME_DIR = os.path.join(BOT_DIR, "runtime")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(RUNTIME_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "bot.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
sys.path.insert(0, BOT_DIR)

from live_api import LiveUpbitAPI
from live_trader import LiveTrader
from strategy_live import get_strategy, Signal, RSIStrategy


class LiveSafetyManager:
    """Enhanced safety for live trading."""

    def __init__(self, config: Dict):
        s = config["safety"]
        self.max_daily_loss_rate = s["max_daily_loss_rate"]       # e.g. -0.03 (tighter for live)
        self.max_mdd_rate = s["max_mdd_rate"]                     # e.g. -0.05
        self.max_single_coin_weight = s["max_single_coin_weight"] # e.g. 0.25
        self.max_positions = s.get("max_positions", 3)            # max concurrent positions
        self.cooldown_cycles = s.get("cooldown_cycles", 3)        # cycles after a sell before buying again
        self.last_sell_cycle: Dict[str, int] = {}                 # market → cycle when sold

        # Forced-sell parameters
        self.forced_sell = s.get("forced_sell", {})
        self.stop_loss_pct: float = self.forced_sell.get("stop_loss_pct", -5.0)
        self.take_profit_pct: float = self.forced_sell.get("take_profit_pct", 10.0)
        self.max_hold_hours: float = self.forced_sell.get("max_hold_hours", 72)
        self.position_entry_time: Dict[str, datetime] = {}        # market → entry timestamp
        self.position_entry_price: Dict[str, float] = {}          # market → entry price
        self.position_peak_price: Dict[str, float] = {}           # market → peak price since entry
        self.emergency_stop = s.get("emergency_stop", False)
        self.cycle_count = 0

        # Cascading early loss cut (V4)
        self.cascading_cut = s.get("cascading_cut", {})
        self.cascading_enabled = self.cascading_cut.get("enabled", True)
        # Default cascade thresholds: (hours, min_profit_pct)
        self.cascade_thresholds = self.cascading_cut.get("thresholds", [
            [2, -1.5],   # 2h: cut if -1.5% (thesis broken)
            [4, 0.0],    # 4h: cut if red (no recovery)
            [8, 0.5],    # 8h: cut if not +0.5% (dead trade)
            [16, 1.0],   # 16h: cut if not +1% (final mercy)
        ])

        # Trailing stop
        self.trailing_stop = s.get("trailing_stop", {})
        self.trailing_enabled = self.trailing_stop.get("enabled", True)
        self.trailing_activation_pct = self.trailing_stop.get("activation_pct", 5.0)   # activate after +5%
        self.trailing_distance_pct = self.trailing_stop.get("distance_pct", 3.0)       # trail by 3%

    def advance_cycle(self):
        self.cycle_count += 1

    def check(self, trader: LiveTrader) -> dict:
        violations = []
        pv = trader.get_portfolio_value()
        krw = trader.get_krw_balance()
        positions = trader.get_positions()

        # Update peak
        if pv > trader.peak_value:
            trader.peak_value = pv

        # 1. Emergency stop
        if self.emergency_stop:
            violations.append("EMERGENCY STOP active")

        # 2. Max positions — log warning but do NOT block trading.
        #    can_buy() already enforces the limit; blocking here would also
        #    prevent SELL signals from executing, causing positions to be
        #    trapped during uptrends.
        if len(positions) >= self.max_positions:
            logger.info("Max positions reached (%d >= %d) — buys blocked, sells still allowed",
                        len(positions), self.max_positions)

        # 3. MDD
        mdd = (trader.peak_value - pv) / trader.peak_value if trader.peak_value > 0 else 0
        if mdd > abs(self.max_mdd_rate):
            violations.append(f"MDD {mdd:.2%} exceeds limit {abs(self.max_mdd_rate):.2%}")

        # 4. Daily loss
        today = datetime.now().strftime("%Y-%m-%d")
        daily_pnl = trader.daily_pnl.get(today, 0.0)
        daily_loss_rate = daily_pnl / trader.initial_balance if trader.initial_balance > 0 else 0
        if daily_loss_rate < self.max_daily_loss_rate:
            violations.append(f"Daily loss {daily_loss_rate:.2%} exceeds limit {self.max_daily_loss_rate:.2%}")

        can = len(violations) == 0
        if not can:
            logger.warning("Safety violations: %s", violations)

        return {
            "can_trade": can,
            "violations": violations,
            "mdd": mdd,
            "daily_loss_rate": daily_loss_rate,
            "positions_count": len(positions),
            "portfolio_value": pv,
        }

    def can_buy(self, market: str, trader: LiveTrader) -> bool:
        """Check if we're allowed to buy a specific market."""
        positions = trader.get_positions()

        # Already have position
        if market in positions:
            return False

        # Max positions
        if len(positions) >= self.max_positions:
            return False

        # Cooldown after selling this market
        if market in self.last_sell_cycle:
            cycles_since = self.cycle_count - self.last_sell_cycle[market]
            if cycles_since < self.cooldown_cycles:
                logger.info("Cooldown active for %s (%d/%d cycles)", market, cycles_since, self.cooldown_cycles)
                return False

        return True

    def on_sell(self, market: str):
        self.last_sell_cycle[market] = self.cycle_count
        self.position_entry_time.pop(market, None)
        self.position_entry_price.pop(market, None)
        self.position_peak_price.pop(market, None)

    def on_buy(self, market: str, entry_price: float):
        self.position_entry_time[market] = datetime.now()
        self.position_entry_price[market] = entry_price
        self.position_peak_price[market] = entry_price

    def update_peak(self, market: str, current_price: float):
        """Update peak price for trailing stop tracking."""
        if market in self.position_peak_price:
            if current_price > self.position_peak_price[market]:
                self.position_peak_price[market] = current_price

    def check_cascading_cut(self, market: str, current_price: float, avg_price: float) -> Optional[str]:
        """Cascading early loss cut (V4). Returns reason if cut needed."""
        if not self.cascading_enabled:
            return None
        if market not in self.position_entry_time:
            return None

        held_seconds = time.time() - self.position_entry_time[market].timestamp()
        held_hours = held_seconds / 3600
        pnl_pct = (current_price - avg_price) / avg_price * 100

        for threshold_hours, min_profit in self.cascade_thresholds:
            if held_hours >= threshold_hours and pnl_pct < min_profit:
                return f"Cascading cut {threshold_hours}h (PnL {pnl_pct:.1f}% < {min_profit:+.1f}%)"

        return None

    def check_trailing_stop(self, market: str, current_price: float, avg_price: float) -> Optional[str]:
        """Trailing stop check. Returns reason if stop triggered."""
        if not self.trailing_enabled:
            return None
        if market not in self.position_peak_price:
            return None

        peak = self.position_peak_price[market]
        pnl_from_peak = (current_price - peak) / peak * 100
        pnl_from_entry = (current_price - avg_price) / avg_price * 100

        # Only activate after reaching activation threshold
        if pnl_from_entry < self.trailing_activation_pct:
            return None

        # Check if price dropped more than trailing distance from peak
        if pnl_from_peak <= -self.trailing_distance_pct:
            return f"Trailing stop (peak {peak:,.0f} → {current_price:,.0f}, -{abs(pnl_from_peak):.1f}%)"

        return None

    def check_forced_sell(self, market: str, trader: LiveTrader, ticker_map: Optional[Dict[str, float]] = None) -> Optional[str]:
        """Check if a position should be force-sold based on stop-loss/take-profit/hold time/cascade/trailing.
        Returns reason string if forced-sell is needed, None otherwise.
        ticker_map: optional pre-fetched {market: trade_price} to avoid redundant API calls."""
        positions = trader.get_positions()
        if market not in positions:
            return None

        avg_price = trader.api.get_average_buy_price(market)
        if not avg_price:
            return None

        # Get current price — use ticker_map if provided, else fetch individually
        if ticker_map and market in ticker_map:
            current_price = ticker_map[market]
        else:
            tickers = trader.api.get_ticker([market])
            if not tickers:
                return None
            current_price = tickers[0]["trade_price"]

        pnl_pct = (current_price - avg_price) / avg_price * 100

        # Update peak for trailing stop
        self.update_peak(market, current_price)

        # Stop-loss (hard floor)
        if pnl_pct <= self.stop_loss_pct:
            return f"Stop-loss triggered ({pnl_pct:.1f}% <= {self.stop_loss_pct:.1f}%)"

        # Take-profit
        if pnl_pct >= self.take_profit_pct:
            return f"Take-profit triggered ({pnl_pct:.1f}% >= {self.take_profit_pct:.1f}%)"

        # Cascading early loss cut (V4) — checked before max hold
        cascade_reason = self.check_cascading_cut(market, current_price, avg_price)
        if cascade_reason:
            return cascade_reason

        # Trailing stop
        trailing_reason = self.check_trailing_stop(market, current_price, avg_price)
        if trailing_reason:
            return trailing_reason

        # Max hold time (last resort)
        if market in self.position_entry_time:
            held_seconds = time.time() - self.position_entry_time[market].timestamp()
            held_hours = held_seconds / 3600
            if held_hours >= self.max_hold_hours:
                return f"Max hold time exceeded ({held_hours:.1f}h >= {self.max_hold_hours}h)"

        return None


class LiveBot:
    def __init__(self, config_path: str = "config_live.json"):
        if not os.path.isabs(config_path):
            config_path = os.path.join(BOT_DIR, config_path)

        with open(config_path) as f:
            self.config = json.load(f)

        # Assert live trading is explicitly enabled
        assert self.config["safety"]["live_trading_enabled"] is True, \
            "live_trading_enabled MUST be true for live bot"

        self.api = LiveUpbitAPI()
        self.trader = LiveTrader(self.config)
        self.safety = LiveSafetyManager(self.config)
        self.strategy = get_strategy(self.config["strategy"])

        # RSI fallback: config에서 사용 여부/파라미터를 읽음
        rsi_cfg = self.config.get("rsi_fallback", {})
        self.rsi_enabled = rsi_cfg.get("enabled", True)
        self.rsi_strategy = None
        if self.rsi_enabled:
            self.rsi_strategy = RSIStrategy({
                "name": "rsi",
                "params": rsi_cfg.get("params", {"period": 14, "overbought": 70, "oversold": 30, "interval": "60"}),
            })

        self.markets = self.config["live_trading"]["markets"]
        self.cycle_seconds = self.config["live_trading"]["cycle_seconds"]
        self.running = False
        self.last_report_day = None

        # 신호 부재 추적
        self.cycles_without_signal = 0
        self.max_silent_cycles = 120  # 2시간 (60초*120) 경고

        # Initial equity snapshot
        self.trader._snapshot_equity()

        logger.info("=== LIVE BOT initialized ===")
        logger.info("  Strategy: %s", self.config["strategy"]["name"])
        logger.info("  Markets: %s", self.markets)
        logger.info("  Initial KRW: ₩%.0f", self.trader.initial_balance)
        logger.info("  Position size: %.0f%%", self.trader.position_size_pct * 100)
        logger.info("  Max positions: %d", self.safety.max_positions)
        if self.rsi_enabled:
            logger.info("  RSI fallback: ENABLED (oversold<%.0f, overbought>%.0f)",
                        self.rsi_strategy.oversold, self.rsi_strategy.overbought)

    def _market_summary(self) -> str:
        """30사이클마다 호출: 각 종목의 MA10/MA30/MA50 스냅샷을 한 줄로."""
        parts = []
        for market in self.markets:
            try:
                candles = self.api.get_candles(market, "60", 60)
                if len(candles) < 30:
                    continue
                prices = [c["trade_price"] for c in candles]
                ma10 = sum(prices[-10:]) / 10
                ma30 = sum(prices[-30:]) / 30
                ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else ma30
                diff_pct = (ma10 - ma30) / ma30 * 100
                parts.append(f"{market.split('-')[1]}:MA10/30={ma10:,.0f}/{ma30:,.0f}({diff_pct:+.1f}%)")
            except Exception:
                continue
        return " | ".join(parts)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        self.running = True
        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)

        logger.info("=== LIVE bot started === (entering main loop)")
        sys.stderr.flush()
        self._write_status()

        cycle_num = 0
        while self.running:
            cycle_num += 1
            if cycle_num <= 3:
                logger.info(">>> Cycle %d start", cycle_num)
                sys.stderr.flush()
            cycle_start = time.time()
            try:
                self.safety.advance_cycle()

                # 1. Safety check
                safety_result = self.safety.check(self.trader)

                if not safety_result["can_trade"]:
                    logger.warning("Trading halted: %s", safety_result["violations"])
                    self._write_status()
                    time.sleep(self.cycle_seconds)
                    continue

                # 1.5 Forced-sell check: stop-loss / take-profit / max hold time
                positions = self.trader.get_positions()
                # Pre-fetch ticker once for all positions
                ticker_map: Dict[str, float] = {}
                if positions:
                    tickers = self.api.get_ticker(list(positions.keys()))
                    ticker_map = {tk["market"]: tk["trade_price"] for tk in tickers}
                for market in list(positions.keys()):
                    reason = self.safety.check_forced_sell(market, self.trader, ticker_map)
                    if reason:
                        logger.warning("FORCED SELL %s — %s", market, reason)
                        self.trader.sell(market)
                        self.safety.on_sell(market)

                # 2. Generate signals (primary: MA Crossover)
                signals = self.strategy.generate_signals(self.api, self.markets)

                # 2a. RSI fallback: MA 신호가 없으면 RSI도 확인
                if not signals and self.rsi_strategy:
                    try:
                        rsi_signals = self.rsi_strategy.generate_signals(self.api, self.markets)
                        if rsi_signals:
                            logger.info("RSI fallback generated %d signal(s)", len(rsi_signals))
                            signals.extend(rsi_signals)
                    except Exception as e:
                        logger.error("RSI fallback error: %s", e)

                # 2b. 로그: 신호 부재
                if not signals:
                    self.cycles_without_signal += 1
                    if self.cycles_without_signal % 30 == 0:
                        # 30사이클(30분)마다 시장 상태 로깅
                        logger.info(
                            "No signals for %d cycles. Market snapshot: %s",
                            self.cycles_without_signal,
                            self._market_summary(),
                        )
                    if self.cycles_without_signal >= self.max_silent_cycles:
                        logger.warning(
                            "⚠️ No trading signals for %d cycles (>2h). Market may be ranging.",
                            self.cycles_without_signal,
                        )
                else:
                    if self.cycles_without_signal > 0:
                        logger.info("Signal returned after %d silent cycles", self.cycles_without_signal)
                    self.cycles_without_signal = 0

                # 3. Execute — process signals
                positions = self.trader.get_positions()
                acted_this_cycle: Set[str] = set()

                for sig in signals:
                    if sig.market in acted_this_cycle:
                        continue

                    if sig.side == "buy":
                        if self.safety.can_buy(sig.market, self.trader):
                            # Calculate position size based on current KRW
                            krw = self.trader.get_krw_balance()
                            amount = krw * self.trader.position_size_pct
                            if amount >= 5000:
                                logger.info("Signal: BUY %s (%.1f%% conf) — %s", sig.market, sig.confidence * 100, sig.reason)
                                result = self.trader.buy(sig.market, amount)
                                if result:
                                    # Get entry price from ticker
                                    tickers = self.api.get_ticker([sig.market])
                                    entry_price = tickers[0]["trade_price"] if tickers else 0
                                    self.safety.on_buy(sig.market, entry_price)
                                acted_this_cycle.add(sig.market)
                            else:
                                logger.info("Signal BUY %s but amount too small (₩%.0f)", sig.market, amount)

                    elif sig.side == "sell":
                        if sig.market in positions:
                            logger.info("Signal: SELL %s (%.1f%% conf) — %s", sig.market, sig.confidence * 100, sig.reason)
                            self.trader.sell(sig.market)
                            self.safety.on_sell(sig.market)
                            acted_this_cycle.add(sig.market)

                # 4. Status
                self._write_status()

                # 5. Daily summary at 08:00 KST
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.hour == 8 and self.last_report_day != today:
                    self._daily_summary()
                    self.last_report_day = today

            except Exception as e:
                logger.error("Cycle error: %s", e, exc_info=True)

            # Sleep
            elapsed = time.time() - cycle_start
            remaining = max(0.5, self.cycle_seconds - elapsed)
            time.sleep(remaining)

        logger.info("=== LIVE bot stopped ===")

    def _daily_summary(self):
        status = self.trader.get_status()
        pv = status["portfolio_value"]
        ret = ((pv - self.trader.initial_balance) / self.trader.initial_balance * 100) if self.trader.initial_balance > 0 else 0
        logger.info("=== DAILY SUMMARY ===")
        logger.info("  Portfolio: ₩%.0f (%.2f%%)", pv, ret)
        logger.info("  KRW: ₩%.0f", status["krw_balance"])
        logger.info("  Positions: %d", status["positions_count"])
        logger.info("  Trades: %d", status["total_trades"])

    def _write_status(self):
        status = self.trader.get_status()
        with open(os.path.join(RUNTIME_DIR, "status.json"), "w") as f:
            json.dump(status, f, indent=2, ensure_ascii=False)

    def _handle_sigint(self, signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        self.running = False


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser(description="Upbit Live Trading Bot")
    p.add_argument("--config", default="config_live.json")
    p.add_argument("--status", action="store_true", help="Print status and exit")
    p.add_argument("--dry-run", action="store_true", help="Simulate signals without trading")
    args = p.parse_args()

    if args.status:
        config_path = args.config
        if not os.path.isabs(config_path):
            config_path = os.path.join(BOT_DIR, config_path)
        with open(config_path) as f:
            cfg = json.load(f)
        trader = LiveTrader(cfg)
        print(json.dumps(trader.get_status(), indent=2, ensure_ascii=False))
        return

    if args.dry_run:
        config_path = args.config
        if not os.path.isabs(config_path):
            config_path = os.path.join(BOT_DIR, config_path)
        with open(config_path) as f:
            cfg = json.load(f)
        api = LiveUpbitAPI()
        strategy = get_strategy(cfg["strategy"])
        markets = cfg["live_trading"]["markets"]
        krw = api.get_krw_balance()
        print(f"DRY RUN — KRW balance: ₩{krw:,.0f}")
        print(f"Markets: {markets}")
        print(f"Strategy: {cfg['strategy']['name']}")
        signals = strategy.generate_signals(api, markets)
        if not signals:
            print("No signals.")
        for sig in signals:
            print(f"  {sig.side.upper()} {sig.market} ({sig.confidence:.1%}) — {sig.reason}")
        return

    bot = LiveBot(args.config)
    bot.run()


if __name__ == "__main__":
    main()
