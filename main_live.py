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
# Logging — rotating, so a long-running bot cannot grow an unbounded log
# (bot.log had reached 109 MiB with a plain FileHandler and no rotation).
# ---------------------------------------------------------------------------
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[
        RotatingFileHandler(os.path.join(LOG_DIR, "bot.log"),
                            maxBytes=20 * 1024 * 1024, backupCount=5,
                            encoding="utf-8"),
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
from strategy_live import get_strategy, Signal, RSIStrategy, MeanReversionStrategy
from performance import PerformanceMetrics


class LiveSafetyManager:
    """Enhanced safety for live trading."""

    def __init__(self, config: Dict, meta_file: Optional[str] = None):
        s = config["safety"]
        self.max_daily_loss_rate = s["max_daily_loss_rate"]       # e.g. -0.03 (tighter for live)
        self.max_mdd_rate = s["max_mdd_rate"]                     # e.g. -0.05
        self.max_single_coin_weight = s["max_single_coin_weight"] # e.g. 0.25
        self.max_positions = s.get("max_positions", 3)            # max concurrent positions
        self.cooldown_cycles = s.get("cooldown_cycles", 3)        # minutes after a sell before buying again
        self.last_sell_cycle: Dict[str, int] = {}                 # market → cycle when sold (in-memory only)
        self.last_sell_time: Dict[str, float] = {}                # market → epoch when sold (persisted)

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

        # Portfolio drawdown circuit breaker (V8.1). Backtest 2024-07→2026-10:
        # a -15% peak-to-trough stop cut max drawdown 35.8% → 15.7% while
        # improving Sharpe 1.52 → 1.70 (walk-forward 4/5 folds unchanged).
        # It force-liquidates the book and pauses new buys for the cooldown,
        # so a losing streak cannot compound. The gate alone only blocks BUYS;
        # this is what actually protects capital when positions are falling.
        self.portfolio_stop_pct = s.get("portfolio_stop_pct", 0.15)
        self.portfolio_stop_cooldown_hours = s.get("portfolio_stop_cooldown_hours", 12)
        self._halt_until_ts: float = 0.0            # epoch until buys are paused
        self._portfolio_stop_file = os.path.join(RUNTIME_DIR, "portfolio_halt.json")
        self._load_portfolio_halt()

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

        # Bear market regime detection (V5)
        self.bear_market = s.get("bear_market", {})
        self.bear_enabled = self.bear_market.get("enabled", True)
        self.btc_ma_short = self.bear_market.get("btc_ma_short", 20)
        self.btc_ma_fast = self.bear_market.get("btc_ma_fast", 50)
        self.btc_ma_slow = self.bear_market.get("btc_ma_slow", 200)
        self.btc_interval = self.bear_market.get("btc_interval", "day")
        self.regimes = self.bear_market.get("regimes", {})
        self.regime_cache_ttl = self.bear_market.get("cache_ttl_seconds", 300)
        # (timestamp, regime, (ema20, ema50, ema200)) — EMAs preserved for ticker-only fallback
        self._regime_cache: tuple = (0, "bull", None)
        self._current_regime: str = "bull"
        self._btc_regime: str = "bull"
        self._regime_score: int = 4
        # Breadth hysteresis: the alt-market breadth swings across the 30%
        # downgrade threshold almost every 10-minute sample (observed 15-44%),
        # so a single reading used to flip the regime — and with it position
        # sizing (15%->10%), the confidence floor (5->6) and max positions
        # (7->6). That produced 200 regime flips and starved buying. Only
        # sustained weakness now downgrades, and a perfect BTC score (4/4)
        # is never overridden by breadth noise.
        self.regime_confirm_samples = self.bear_market.get("confirm_samples", 3)
        self._consecutive_weak_breadth: int = 0

        # Per-position strategy tag + custom exit params (persisted across restarts)
        self.position_strategy: Dict[str, str] = {}
        self.position_custom_exit: Dict[str, dict] = {}
        self._meta_file = meta_file or os.path.join(RUNTIME_DIR, "positions_meta.json")
        self._load_position_meta()

        # Market breadth detection (V6)
        self.market_breadth = s.get("market_breadth", {})
        self.breadth_enabled = self.market_breadth.get("enabled", True)
        self.breadth_ema_period = self.market_breadth.get("ema_period", 50)
        self.breadth_interval = self.market_breadth.get("interval", "60")
        self.breadth_thresholds = self.market_breadth.get("thresholds", [[20, 1]])
        self.breadth_cache_ttl = self.market_breadth.get("cache_ttl_seconds", 600)
        # Breadth-based regime adjustment: the regime comes from BTC, but we trade
        # alts — when most alts are trending up (alt season) the regime is upgraded
        # one level, and downgraded when most are trending down.
        self.breadth_upgrade_min = self.bear_market.get("breadth_upgrade_min", 65)
        self.breadth_downgrade_max = self.bear_market.get("breadth_downgrade_max", 35)
        # Start at 50 (no modifier) until first real measurement
        self._breadth_cache: tuple = (0, 50.0)   # (timestamp, breadth_pct)
        self._current_breadth: float = 50.0      # % of coins above EMA50

    def advance_cycle(self):
        self.cycle_count += 1

    # ------------------------------------------------------------------
    # Portfolio drawdown circuit breaker (V8.1)
    # ------------------------------------------------------------------
    def _load_portfolio_halt(self):
        """Restore a running buy-pause across restarts (otherwise a restart
        would immediately re-enter the drawdown the stop just avoided)."""
        try:
            with open(self._portfolio_stop_file) as f:
                d = json.load(f)
            self._halt_until_ts = float(d.get("halt_until_ts", 0.0))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Failed to load portfolio halt state: %s", e)

    def _save_portfolio_halt(self):
        try:
            with open(self._portfolio_stop_file, "w") as f:
                json.dump({"halt_until_ts": self._halt_until_ts}, f)
        except Exception as e:
            logger.warning("Failed to save portfolio halt state: %s", e)

    def buys_paused(self) -> bool:
        return time.time() < self._halt_until_ts

    def check_portfolio_stop(self, trader: LiveTrader) -> bool:
        """If the portfolio is this far below its running peak, liquidate
        everything and pause new buys for the cooldown. Returns True if a stop
        was triggered this call."""
        if self.portfolio_stop_pct is None or self.portfolio_stop_pct <= 0:
            return False
        if self.buys_paused():
            return False  # already halted — do not re-trigger every cycle
        pv = trader.get_portfolio_value()
        peak = trader.peak_value
        if peak <= 0:
            return False
        dd = (peak - pv) / peak
        if dd < self.portfolio_stop_pct:
            return False

        logger.warning(
            "PORTFOLIO STOP: drawdown %.1f%% >= %.1f%% (peak ₩%.0f → ₩%.0f) — "
            "liquidating and pausing buys for %dh",
            dd * 100, self.portfolio_stop_pct * 100, peak, pv,
            self.portfolio_stop_cooldown_hours,
        )
        for market in list(trader.get_positions().keys()):
            try:
                r = trader.sell(market)
                if r and r.get("confirmed", True):
                    self.on_sell(market)
            except Exception as e:
                logger.error("Portfolio-stop sell failed for %s: %s", market, e)
        # reset the peak baseline so MDD is measured from here on
        trader.peak_value = pv
        trader._save_state()
        self._halt_until_ts = time.time() + self.portfolio_stop_cooldown_hours * 3600
        self._save_portfolio_halt()
        return True

    # ------------------------------------------------------------------
    # Position metadata persistence (restart-safe trailing/cascade/max-hold)
    # ------------------------------------------------------------------
    def _load_position_meta(self):
        try:
            with open(self._meta_file) as f:
                meta = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning("Failed to load position metadata: %s", e)
            return
        for market, m in meta.items():
            if market == "cooldowns":
                # Wall-clock sell times, so a restart does not clear the cooldown
                self.last_sell_time = {k: float(v) for k, v in m.items()}
                continue
            try:
                self.position_entry_time[market] = datetime.fromisoformat(m["entry_time"])
            except Exception:
                self.position_entry_time[market] = datetime.now()
            self.position_entry_price[market] = m.get("entry_price", 0.0)
            self.position_peak_price[market] = m.get("peak_price", m.get("entry_price", 0.0))
            self.position_strategy[market] = m.get("strategy", "unknown")
            if m.get("custom_exit"):
                self.position_custom_exit[market] = m["custom_exit"]
        if meta:
            logger.info("Restored position metadata: %s", list(meta.keys()))

    def _save_position_meta(self):
        meta = {}
        for market, ts in self.position_entry_time.items():
            meta[market] = {
                "entry_time": ts.isoformat(),
                "entry_price": self.position_entry_price.get(market, 0.0),
                "peak_price": self.position_peak_price.get(market, 0.0),
                "strategy": self.position_strategy.get(market, "unknown"),
                "custom_exit": self.position_custom_exit.get(market),
            }
        if self.last_sell_time:
            meta["cooldowns"] = dict(self.last_sell_time)
        try:
            with open(self._meta_file, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save position metadata: %s", e)

    def sync_positions(self, trader: LiveTrader):
        """Reconcile tracked metadata with actual exchange positions.
        Adopts untracked positions (restart / manual buys) and drops stale entries."""
        positions = trader.get_positions()
        changed = False
        for market in positions:
            if market not in self.position_entry_time:
                avg = trader.api.get_average_buy_price(market) or 0.0
                self.position_entry_time[market] = datetime.now()
                self.position_entry_price[market] = avg
                self.position_peak_price[market] = avg
                self.position_strategy[market] = "unknown"
                logger.info("Adopted untracked position %s (avg_buy=₩%.2f)", market, avg)
                changed = True
        for market in list(self.position_entry_time.keys()):
            if market not in positions:
                logger.info("Position %s no longer on exchange — dropping metadata", market)
                self.on_sell(market)
                changed = False  # on_sell already saved
        if changed:
            self._save_position_meta()

    @staticmethod
    def _classify_regime(price: float, ema_short: float, ema_fast: float, ema_slow: float):
        """4-point trend score on BTC daily EMAs → (regime, score).

        Macro legs (price vs EMA200, EMA50 vs EMA200) alone react too slowly:
        a multi-week -15% correction can stay 'bull' until EMA200 finally breaks.
        Adding short-term legs (price vs EMA20, EMA20 vs EMA50) downgrades the
        regime step-by-step as a decline deepens, and upgrades it gradually
        during recovery.

        bull requires score >= 3 AND price above EMA20 — once price loses the
        20-day line the regime is at best neutral, regardless of macro structure.
          score >= 3 (+ price > EMA20) → bull, 2-3 → neutral, 1-0 → bear
        """
        score = 0
        if price > ema_slow:
            score += 1
        if ema_fast > ema_slow:
            score += 1
        if price > ema_short:
            score += 1
        if ema_short > ema_fast:
            score += 1

        if score >= 3 and price > ema_short:
            return "bull", score
        if score >= 2:
            return "neutral", score
        return "bear", score

    def _apply_breadth_modifier(self, regime: str) -> str:
        """Adjust the BTC-based regime by alt-market breadth.

        The bot trades ~50 altcoins, so BTC alone misclassifies alt seasons
        (BTC flat/lagging while alts rally → 'neutral'/'bear' starves buying)
        and blow-off tops (BTC strong while alts bleed). Strong breadth
        upgrades one level, weak breadth downgrades one level.

        Two guards keep this from becoming a noise amplifier (2026-09-30:
        200 regime flips in a week, breadth oscillating 15-44% across the 30%
        threshold — the bot re-sized, re-filtered and re-bought on noise):

        1. BTC-score gate — when BTC itself scores 4/4 the macro trend is
           unambiguous. Breadth of a bleeding alt market may not downgrade it.
        2. Hysteresis + deadband — a downgrade needs `regime_confirm_samples`
           consecutive weak readings, and breadth must recover above the band
           before it can trigger again.
        """
        if not self.breadth_enabled:
            return regime
        levels = ["bear", "neutral", "bull"]
        idx = levels.index(regime) if regime in levels else 1

        weak = self._current_breadth <= self.breadth_downgrade_max
        strong = self._current_breadth >= self.breadth_upgrade_min

        # Deadband: once weak, require a recovery above the downgrade threshold
        # before the weak branch can re-arm.
        if weak:
            self._consecutive_weak_breadth += 1
        else:
            self._consecutive_weak_breadth = 0

        if strong:
            idx = min(len(levels) - 1, idx + 1)
        elif weak and self._consecutive_weak_breadth >= self.regime_confirm_samples:
            # BTC-score gate: never downgrade an unambiguous macro uptrend.
            if self._regime_score >= 4 and self._btc_regime == "bull":
                logger.debug(
                    "Breadth %.0f%% weak but BTC score 4/4 — downgrade suppressed",
                    self._current_breadth,
                )
            else:
                idx = max(0, idx - 1)
        return levels[idx]

    def detect_market_regime(self, api) -> str:
        """Detect market regime using BTC EMA20/EMA50/EMA200 on DAILY candles.
        Returns 'bull', 'neutral', or 'bear'. Cached for regime_cache_ttl seconds.
        On API failure, falls back to BTC ticker vs last-known EMA values."""
        if not self.bear_enabled:
            return "bull"

        now = time.time()
        cache_ts, cached_regime, cached_emas = self._regime_cache
        if now - cache_ts < self.regime_cache_ttl:
            return cached_regime

        try:
            if self.btc_interval == "day":
                candles = api.get_day_candles("KRW-BTC", max(self.btc_ma_slow + 20, 220))
            else:
                candles = api.get_candles("KRW-BTC", self.btc_interval, max(self.btc_ma_slow + 10, 210))
            if len(candles) < self.btc_ma_slow + 1:
                logger.warning("BTC candles insufficient for regime detection (%d < %d)", len(candles), self.btc_ma_slow + 1)
                return self._current_regime

            # Upbit API returns newest-first; candles[0] is the still-forming candle.
            # Use it only as the current price; compute MAs on completed candles.
            live_price = candles[0]["trade_price"]
            prices = [c["trade_price"] for c in candles[1:]]
            prices.reverse()

            # Calculate EMAs
            def _ema(data, period):
                if len(data) < period:
                    return None
                multiplier = 2 / (period + 1)
                ema = sum(data[:period]) / period
                for i in range(period, len(data)):
                    ema = (data[i] - ema) * multiplier + ema
                return ema

            ema20 = _ema(prices, self.btc_ma_short)
            ema50 = _ema(prices, self.btc_ma_fast)
            ema200 = _ema(prices, self.btc_ma_slow)
            current_price = live_price

            if ema20 is None or ema50 is None or ema200 is None:
                return self._current_regime

            btc_regime, score = self._classify_regime(current_price, ema20, ema50, ema200)
            regime = self._apply_breadth_modifier(btc_regime)

            self._regime_cache = (now, regime, (ema20, ema50, ema200))
            self._regime_score = score
            self._btc_regime = btc_regime
            if regime != self._current_regime:
                logger.info(
                    "Market regime changed: %s → %s (BTC %s score %d/4, breadth %.0f%%, BTC=₩%.0f, EMA%d=₩%.0f, EMA%d=₩%.0f, EMA%d=₩%.0f)",
                    self._current_regime, regime, btc_regime, score, self._current_breadth,
                    current_price,
                    self.btc_ma_short, ema20, self.btc_ma_fast, ema50, self.btc_ma_slow, ema200,
                )
            self._current_regime = regime
            return regime

        except Exception as e:
            logger.warning("Market regime detection failed (candles): %s", e)
            # Fallback: use BTC ticker + last-known EMA values
            if cached_emas is not None:
                try:
                    tickers = api.get_ticker(["KRW-BTC"])
                    if tickers:
                        btc_price = tickers[0]["trade_price"]
                        btc_regime, score = self._classify_regime(btc_price, *cached_emas)
                        regime = self._apply_breadth_modifier(btc_regime)
                        # Don't update EMA cache — only refresh timestamp + regime
                        self._regime_cache = (now, regime, cached_emas)
                        self._regime_score = score
                        self._btc_regime = btc_regime
                        if regime != self._current_regime:
                            logger.info(
                                "Market regime changed (ticker fallback): %s → %s (BTC %s score %d/4, breadth %.0f%%, BTC=₩%.0f)",
                                self._current_regime, regime, btc_regime, score,
                                self._current_breadth, btc_price,
                            )
                        self._current_regime = regime
                        return regime
                except Exception as e2:
                    logger.warning("Ticker fallback also failed: %s", e2)
            return self._current_regime

    def detect_market_breadth(self, api, markets: list) -> float:
        """Calculate market breadth: % of markets above their EMA50 on 1h candles.
        Returns breadth percentage (0-100). Cached for breadth_cache_ttl seconds."""
        if not self.breadth_enabled:
            return 100.0

        now = time.time()
        cache_ts, cached_breadth = self._breadth_cache
        if now - cache_ts < self.breadth_cache_ttl:
            return cached_breadth

        try:
            above_ema = 0
            total = 0
            for market in markets:
                try:
                    candles = api.get_candles(market, self.breadth_interval, self.breadth_ema_period + 6)
                    # Drop the still-forming newest candle; reverse to chronological
                    candles = candles[1:]
                    if len(candles) < self.breadth_ema_period:
                        continue
                    prices = [c["trade_price"] for c in reversed(candles)]

                    # Calculate EMA
                    multiplier = 2 / (self.breadth_ema_period + 1)
                    ema = sum(prices[:self.breadth_ema_period]) / self.breadth_ema_period
                    for i in range(self.breadth_ema_period, len(prices)):
                        ema = (prices[i] - ema) * multiplier + ema

                    if prices[-1] > ema:
                        above_ema += 1
                    total += 1
                except Exception:
                    continue

            if total == 0:
                return self._current_breadth

            breadth = (above_ema / total) * 100
            self._breadth_cache = (now, breadth)

            if abs(breadth - self._current_breadth) > 5:
                logger.info(
                    "Market breadth: %.1f%% (%d/%d markets above EMA%d)",
                    breadth, above_ema, total, self.breadth_ema_period,
                )
            self._current_breadth = breadth
            return breadth

        except Exception as e:
            logger.warning("Market breadth detection failed: %s", e)
            return self._current_breadth

    def _get_effective_limits(self):
        """Get regime-aware max_positions and position_size_pct, with breadth modifier."""
        regime_cfg = self.regimes.get(self._current_regime, {})
        effective_max = regime_cfg.get("max_positions", self.max_positions)
        effective_size = regime_cfg.get("position_size_pct", 0.15)
        effective_min_conf = regime_cfg.get("min_confidence", 5)

        # Apply market breadth modifier (V6)
        if self.breadth_enabled:
            breadth = self._current_breadth
            reduction = 0
            for threshold, reduce_by in self.breadth_thresholds:
                if breadth < threshold:
                    reduction = max(reduction, reduce_by)
            if reduction > 0:
                effective_max = max(1, effective_max - reduction)
                logger.debug(
                    "Breadth modifier: %.1f%% < thresholds → max_positions reduced by %d (now %d)",
                    breadth, reduction, effective_max,
                )

        return effective_max, effective_size, effective_min_conf

    def check(self, trader: LiveTrader) -> dict:
        violations = []
        pv = trader.get_portfolio_value()
        krw = trader.get_krw_balance()
        positions = trader.get_positions()

        # Update peak
        trader.record_portfolio_value(pv)

        # 1. Emergency stop
        if self.emergency_stop:
            violations.append("EMERGENCY STOP active")

        # 2. Max positions — log warning but do NOT block trading.
        #    can_buy() already enforces the limit; blocking here would also
        #    prevent SELL signals from executing, causing positions to be
        #    trapped during uptrends.
        effective_max, _, _ = self._get_effective_limits()
        if len(positions) >= effective_max:
            logger.info("Max positions reached (%d >= %d, regime=%s) — buys blocked, sells still allowed",
                        len(positions), effective_max, self._current_regime)

        # 3. MDD
        mdd = (trader.peak_value - pv) / trader.peak_value if trader.peak_value > 0 else 0
        if mdd > abs(self.max_mdd_rate):
            # Deadlock guard: with no open positions there is nothing left to
            # recover the drawdown, so a halt here would freeze the bot forever
            # (observed 2026-09-22 → 2026-09-25).
            #
            # The guard releases the BUY block only. It must NOT rewrite the
            # peak: peak_value is the MDD reference for reporting and is now
            # persisted, so falsifying it hid the drawdown and let every restart
            # erase the metric (observed 2026-09-28 → 2026-09-30).
            # Re-entry risk while flat is already bounded by the daily-loss
            # limit and the regime/confidence filters.
            if not positions and pv > 0:
                logger.warning(
                    "MDD %.2f%% exceeds limit %.2f%% with no open positions — "
                    "deadlock guard releases buys (peak ₩%.0f retained for reporting)",
                    mdd * 100, abs(self.max_mdd_rate) * 100, trader.peak_value,
                )
            else:
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

        # Regime-aware max positions
        effective_max, _, _ = self._get_effective_limits()

        # Max positions
        if len(positions) >= effective_max:
            logger.info("Max positions reached (%d >= %d, regime=%s) — buys blocked",
                        len(positions), effective_max, self._current_regime)
            return False

        # Cooldown after selling this market.
        # Stored as a wall-clock timestamp, not a cycle counter: the counter was
        # reset by every restart, which is how WLD was re-bought 1h after a
        # stop-loss and NEAR 1.2h after a cascading cut (2026-09-29/30).
        if market in self.last_sell_time:
            elapsed_min = (time.time() - self.last_sell_time[market]) / 60
            if elapsed_min < self.cooldown_cycles:
                logger.debug("Cooldown active for %s (%.0f/%.0f min)",
                             market, elapsed_min, self.cooldown_cycles)
                return False

        return True

    def on_sell(self, market: str):
        self.last_sell_time[market] = time.time()
        self.last_sell_cycle[market] = self.cycle_count
        self.position_entry_time.pop(market, None)
        self.position_entry_price.pop(market, None)
        self.position_peak_price.pop(market, None)
        self.position_strategy.pop(market, None)
        self.position_custom_exit.pop(market, None)
        self._save_position_meta()

    def on_buy(self, market: str, entry_price: float, strategy: str = "trendrider",
               custom_exit: Optional[dict] = None):
        self.position_entry_time[market] = datetime.now()
        self.position_entry_price[market] = entry_price
        self.position_peak_price[market] = entry_price
        self.position_strategy[market] = strategy
        if custom_exit:
            self.position_custom_exit[market] = custom_exit
        self._save_position_meta()

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

        # Per-strategy exit overrides (e.g. mean-reversion scalps use tighter TP/SL)
        ce = self.position_custom_exit.get(market) or {}
        stop_loss_pct = ce.get("stop_loss_pct", self.stop_loss_pct)
        take_profit_pct = ce.get("take_profit_pct", self.take_profit_pct)
        max_hold_hours = ce.get("max_hold_hours", self.max_hold_hours)

        # Stop-loss (hard floor)
        if pnl_pct <= stop_loss_pct:
            return f"Stop-loss triggered ({pnl_pct:.1f}% <= {stop_loss_pct:.1f}%)"

        # Take-profit
        if pnl_pct >= take_profit_pct:
            return f"Take-profit triggered ({pnl_pct:.1f}% >= {take_profit_pct:.1f}%)"

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
            if held_hours >= max_hold_hours:
                return f"Max hold time exceeded ({held_hours:.1f}h >= {max_hold_hours}h)"

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
        self.performance = PerformanceMetrics(self.trader)

        # RSI fallback: config에서 사용 여부/파라미터를 읽음
        rsi_cfg = self.config.get("rsi_fallback", {})
        self.rsi_enabled = rsi_cfg.get("enabled", True)
        self.rsi_strategy = None
        if self.rsi_enabled:
            self.rsi_strategy = RSIStrategy({
                "name": "rsi",
                "params": rsi_cfg.get("params", {"period": 14, "overbought": 70, "oversold": 30, "interval": "60"}),
            })

        # Mean reversion strategy (V7) — bear/ranging market용
        mr_cfg = self.config.get("mean_reversion", {})
        self.mr_enabled = mr_cfg.get("enabled", True)
        self.mr_regimes = mr_cfg.get("regimes", ["neutral", "bear"])
        self.mr_strategy = None
        if self.mr_enabled:
            self.mr_strategy = MeanReversionStrategy({
                "name": "mean_reversion",
                "params": mr_cfg.get("params", {}),
            })

        self.markets = self.config["live_trading"]["markets"]
        # Delisted-market guard: Upbit returns HTTP 404 for coins that were
        # delisted (e.g. KRW-AERGO, KRW-AQT) while they linger in the config
        # universe. Each cycle retried them 3x and the resulting burst of
        # requests also tripped the 429 rate limiter, slowing every cycle.
        # Drop any market the exchange no longer lists, before trading starts.
        try:
            listed = {m["market"] for m in self.api.get_market_list()}
            if listed:
                removed = [m for m in self.markets if m not in listed]
                if removed:
                    logger.warning("Pruned delisted markets from universe: %s", removed)
                    self.markets = [m for m in self.markets if m in listed]
                    logger.info("Universe reduced to %d markets", len(self.markets))
        except Exception as e:
            logger.warning("Could not validate market universe against exchange: %s", e)
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
        if self.safety.bear_enabled:
            logger.info("  Bear market filter: ENABLED (bull=%dpos/%.0f%%, neutral=%dpos/%.0f%%, bear=%dpos/%.0f%%)",
                        self.safety.regimes.get("bull", {}).get("max_positions", 7),
                        self.safety.regimes.get("bull", {}).get("position_size_pct", 0.15) * 100,
                        self.safety.regimes.get("neutral", {}).get("max_positions", 4),
                        self.safety.regimes.get("neutral", {}).get("position_size_pct", 0.10) * 100,
                        self.safety.regimes.get("bear", {}).get("max_positions", 2),
                        self.safety.regimes.get("bear", {}).get("position_size_pct", 0.05) * 100)
        if self.rsi_enabled:
            logger.info("  RSI fallback: ENABLED (oversold<%.0f, overbought>%.0f)",
                        self.rsi_strategy.oversold, self.rsi_strategy.overbought)
        if self.safety.breadth_enabled:
            logger.info("  Market breadth: ENABLED (EMA%d, thresholds: %s)",
                        self.safety.breadth_ema_period,
                        ", ".join(f"<{t}%→-{r}pos" for t, r in self.safety.breadth_thresholds))
        if self.mr_enabled:
            logger.info("  Mean reversion: ENABLED (RSI<%.0f→buy, RSI>%.0f→sell, regimes=%s, size=%.0f%%)",
                        self.mr_strategy.rsi_oversold, self.mr_strategy.rsi_exit,
                        ",".join(self.mr_regimes), self.mr_strategy.position_size_pct * 100)

    def _market_summary(self) -> str:
        """30사이클마다 호출: 각 종목의 MA10/MA30/MA50 스냅샷을 한 줄로."""
        parts = []
        for market in self.markets:
            try:
                candles = self.api.get_candles(market, "60", 60)
                if len(candles) < 30:
                    continue
                # Upbit API returns newest-first; reverse to chronological
                prices = [c["trade_price"] for c in reversed(candles)]
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

                # 0. Market breadth (V6) — computed BEFORE regime detection since
                #    it feeds the breadth modifier; every 10 cycles (rate limits)
                if cycle_num % 10 == 1:
                    self.safety.detect_market_breadth(self.api, self.markets)

                # 0.5 Market regime detection (V5, BTC daily + breadth modifier)
                self.safety.detect_market_regime(self.api)
                # Periodic heartbeat so the operator can see the current judgment
                if cycle_num % 30 == 1:
                    logger.info("Market regime: %s (BTC %s score %d/4, breadth %.1f%%)",
                                self.safety._current_regime, self.safety._btc_regime,
                                self.safety._regime_score, self.safety._current_breadth)

                # 1. Safety check — violations block BUYS only; exits/sells must
                #    keep running so losing positions aren't trapped.
                safety_result = self.safety.check(self.trader)
                buys_halted = not safety_result["can_trade"]
                if buys_halted:
                    logger.warning("Buys halted: %s (exits still active)", safety_result["violations"])

                # 1.2 Reconcile position metadata with exchange (restart-safe)
                self.safety.sync_positions(self.trader)

                # 1.3 Portfolio drawdown circuit breaker (V8.1) — the gate blocks
                #     BUYS, but this is what actually protects capital when
                #     already-held positions are falling. Liquidates the book and
                #     pauses buys; must run BEFORE the forced-sell/signal loops.
                self.safety.check_portfolio_stop(self.trader)
                if self.safety.buys_paused():
                    buys_halted = True
                    logger.warning("Buys paused by portfolio stop until %s",
                                   datetime.fromtimestamp(self.safety._halt_until_ts)
                                   .strftime("%Y-%m-%d %H:%M"))

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
                        result = self.trader.sell(market)
                        if result and result.get("confirmed", True):
                            self.safety.on_sell(market)
                        else:
                            logger.warning("SELL %s failed/unconfirmed — keeping position metadata", market)
                # Persist updated peak prices for trailing stops
                self.safety._save_position_meta()

                # 2. Generate signals (primary: TrendRider)
                signals = self.strategy.generate_signals(self.api, self.markets)
                for sig in signals:
                    sig.params.setdefault("strategy", "trendrider")

                # 2a. Mean reversion (V7): neutral/bear regime에서 TrendRider 신호 없을 때
                if not signals and self.mr_strategy and self.safety._current_regime in self.mr_regimes:
                    try:
                        mr_signals = self.mr_strategy.generate_signals(self.api, self.markets)
                        if mr_signals:
                            logger.info("Mean reversion generated %d signal(s) (regime=%s)", len(mr_signals), self.safety._current_regime)
                            for sig in mr_signals:
                                sig.params["strategy"] = "mean_reversion"
                            signals.extend(mr_signals)
                    except Exception as e:
                        logger.error("Mean reversion error: %s", e)

                # 2b. RSI fallback: 여전히 신호 없으면 RSI도 확인
                #     (bear regime에서는 falling-knife 매수 방지를 위해 sell 신호만 허용)
                if not signals and self.rsi_strategy:
                    try:
                        rsi_signals = self.rsi_strategy.generate_signals(self.api, self.markets)
                        for sig in rsi_signals:
                            sig.params["strategy"] = "rsi_fallback"
                        if self.safety._current_regime == "bear":
                            rsi_signals = [s for s in rsi_signals if s.side == "sell"]
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
                    sig_strategy = sig.params.get("strategy", "trendrider")

                    if sig.side == "buy":
                        if buys_halted:
                            continue
                        if not self.safety.can_buy(sig.market, self.trader):
                            continue

                        _, effective_size, effective_min_conf = self.safety._get_effective_limits()

                        # Regime-aware confidence filter — compares the strategy's
                        # 0-10 confidence_score against the regime minimum.
                        conf_score = sig.params.get("confidence_score")
                        if conf_score is not None and conf_score < effective_min_conf:
                            logger.info("Signal BUY %s rejected — score %d/10 < %d (regime=%s)",
                                        sig.market, conf_score, effective_min_conf, self.safety._current_regime)
                            continue

                        # Strategy-specific sizing + exit rules
                        custom_exit = None
                        size_pct = effective_size
                        if sig_strategy == "mean_reversion" and self.mr_strategy:
                            mr_open = sum(1 for s in self.safety.position_strategy.values()
                                          if s == "mean_reversion")
                            if mr_open >= self.mr_strategy.max_positions:
                                logger.info("Signal BUY %s rejected — MR positions full (%d)",
                                            sig.market, mr_open)
                                continue
                            size_pct = self.mr_strategy.position_size_pct
                            custom_exit = {
                                "take_profit_pct": self.mr_strategy.take_profit_pct,
                                "stop_loss_pct": self.mr_strategy.stop_loss_pct,
                                "max_hold_hours": self.mr_strategy.max_hold_hours,
                            }

                        # Size on portfolio value (not just remaining cash), capped by cash
                        krw = self.trader.get_krw_balance()
                        pv = safety_result.get("portfolio_value", krw)
                        amount = min(pv * size_pct, krw * 0.98)

                        if amount >= 5000:
                            logger.info("Signal: BUY %s (%.1f%% conf, %s) — %s",
                                        sig.market, sig.confidence * 100, sig_strategy, sig.reason)
                            result = self.trader.buy(sig.market, amount)
                            if result and result.get("confirmed", True):
                                # Get entry price from ticker (fallback: exchange avg buy price)
                                tickers = self.api.get_ticker([sig.market])
                                entry_price = tickers[0]["trade_price"] if tickers else \
                                    (self.api.get_average_buy_price(sig.market) or 0)
                                self.safety.on_buy(sig.market, entry_price,
                                                   strategy=sig_strategy, custom_exit=custom_exit)
                            acted_this_cycle.add(sig.market)
                        else:
                            logger.info("Signal BUY %s but amount too small (₩%.0f)", sig.market, amount)

                    elif sig.side == "sell":
                        if sig.market not in positions:
                            continue
                        # Exit scoping: a strategy may only close its own positions.
                        # Unknown (adopted) positions are managed by the primary
                        # strategy. gatemomentum owns the WHOLE book (it is a
                        # portfolio rotator that emits sells for every market it
                        # does not want to hold), so it may close any position.
                        pos_strategy = self.safety.position_strategy.get(sig.market, "unknown")
                        if not (pos_strategy == sig_strategy or
                                (pos_strategy == "unknown" and sig_strategy == "trendrider") or
                                sig_strategy == "gatemomentum"):
                            logger.debug("SELL %s from %s skipped — position owned by %s",
                                         sig.market, sig_strategy, pos_strategy)
                            continue
                        logger.info("Signal: SELL %s (%.1f%% conf, %s) — %s",
                                    sig.market, sig.confidence * 100, sig_strategy, sig.reason)
                        result = self.trader.sell(sig.market)
                        if result and result.get("confirmed", True):
                            self.safety.on_sell(sig.market)
                        else:
                            logger.warning("SELL %s failed/unconfirmed — keeping position metadata", sig.market)
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
        report = self.performance.build_report()
        pv = status["portfolio_value"]
        ret = ((pv - self.trader.initial_balance) / self.trader.initial_balance * 100) if self.trader.initial_balance > 0 else 0
        wr = report["win_rate"]
        mdd = report["mdd"]
        fees = report["fees"]
        trades = report["trades"]
        logger.info("=== DAILY SUMMARY ===")
        logger.info("  Portfolio: ₩%.0f (%.2f%%)", pv, ret)
        logger.info("  KRW: ₩%.0f", status["krw_balance"])
        logger.info("  Positions: %d", status["positions_count"])
        logger.info("  Trades: %d (%dB/%dS)", trades["total_trades"], trades["buys"], trades["sells"])
        logger.info("  Realized PnL: ₩%.0f", trades["realized_pnl_total"])
        logger.info("  Win Rate: %.1f%% (%dW/%dL)", wr["win_rate_pct"], wr["wins"], wr["losses"])
        logger.info("  MDD: %.2f%%", mdd["mdd_pct"])
        logger.info("  Fees: ₩%.2f", fees["total_fees"])

    def _write_status(self):
        status = self.trader.get_status()
        # Market judgment snapshot — lets the operator verify regime detection at a glance
        status["market_regime"] = self.safety._current_regime
        status["market_regime_btc"] = self.safety._btc_regime
        status["market_regime_score"] = self.safety._regime_score
        status["market_breadth_pct"] = round(self.safety._current_breadth, 1)
        # Merge performance report into status
        try:
            report = self.performance.build_report()
            status["performance"] = {
                "returns": report["returns"],
                "mdd": report["mdd"],
                "win_rate": report["win_rate"],
                "fees": report["fees"],
                "trades": report["trades"],
            }
        except Exception as e:
            logger.warning("Performance report failed: %s", e)
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
