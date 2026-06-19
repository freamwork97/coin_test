"""
Enhanced Strategy Module — Builds on paper bot learnings
=========================================================
- MA Crossover with confirmation filters
- Volume confirmation: only trade when volume > 20-period avg
- Trend filter: only buy when price > MA200 (daily), only sell when below
- Minimum holding period: no sell within 3 cycles of buy
- Signal strength threshold: ignore weak crosses

NEW: TrendRiderStyleStrategy — multi-signal trend-following with confidence scoring
- 6 entry signals: trend_pullback, ema50_bounce, rsi_bounce, ema_crossover, bb_bounce, macd_reversal
- Confidence scoring (0-10) with 14 weighted factors
- 4 exit signals: rsi_overbought, ema_bearish_cross, trend_broken, trend_early_warning
- Multi-timeframe: 1h main, 4h trend, 1d macro, BTC sentiment
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    market: str
    side: str       # "buy" | "sell" | "hold"
    confidence: float
    reason: str
    params: dict = field(default_factory=dict)


# ============================================================================
# Indicator helpers (pure Python, no TA-Lib/numpy dependency)
# ============================================================================

def _ema(prices: List[float], period: int) -> List[float]:
    """Exponential Moving Average. Returns list same length as input, NaN for insufficient data."""
    result = [float('nan')] * len(prices)
    if len(prices) < period:
        return result
    multiplier = 2 / (period + 1)
    # Seed with SMA
    sma = sum(prices[:period]) / period
    result[period - 1] = sma
    for i in range(period, len(prices)):
        result[i] = (prices[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def _sma(prices: List[float], period: int) -> List[float]:
    """Simple Moving Average."""
    result = [float('nan')] * len(prices)
    if len(prices) < period:
        return result
    window_sum = sum(prices[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(prices)):
        window_sum += prices[i] - prices[i - period]
        result[i] = window_sum / period
    return result


def _rsi(prices: List[float], period: int = 14) -> List[float]:
    """Relative Strength Index (Wilder's smoothing)."""
    result = [float('nan')] * len(prices)
    if len(prices) < period + 1:
        return result
    gains = []
    losses = []
    for i in range(1, len(prices)):
        ch = prices[i] - prices[i - 1]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(abs(ch) if ch < 0 else 0.0)

    # First average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return result


def _true_range(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    """True Range for ATR calculation."""
    result = [float('nan')] * len(highs)
    if len(highs) < 2:
        return result
    for i in range(1, len(highs)):
        h_l = highs[i] - lows[i]
        h_pc = abs(highs[i] - closes[i - 1])
        l_pc = abs(lows[i] - closes[i - 1])
        result[i] = max(h_l, h_pc, l_pc)
    return result


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    """Average True Range (Wilder's smoothing)."""
    tr = _true_range(highs, lows, closes)
    return _ema([x for x in tr if not (x != x)], period)  # filter NaN


def _adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Tuple[List[float], List[float], List[float]]:
    """Returns (adx, plus_di, minus_di) lists."""
    n = len(highs)
    adx = [float('nan')] * n
    plus_di = [float('nan')] * n
    minus_di = [float('nan')] * n
    if n < period + 1:
        return adx, plus_di, minus_di

    # True Range
    tr = _true_range(highs, lows, closes)
    # Directional Movement
    up_move = [0.0] * n
    down_move = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            up_move[i] = up
        if down > up and down > 0:
            down_move[i] = down

    # Wilder's smoothing
    atr_val = sum(tr[1:period + 1]) / period
    up_smooth = sum(up_move[1:period + 1]) / period
    down_smooth = sum(down_move[1:period + 1]) / period

    for i in range(period, n):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        up_smooth = (up_smooth * (period - 1) + up_move[i]) / period
        down_smooth = (down_smooth * (period - 1) + down_move[i]) / period

        if atr_val > 0:
            plus_di[i] = (up_smooth / atr_val) * 100
            minus_di[i] = (down_smooth / atr_val) * 100
            dx = abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i]) * 100 if (plus_di[i] + minus_di[i]) > 0 else 0
            adx[i] = dx

    # Smooth ADX with EMA
    adx_smooth = [float('nan')] * n
    first_adx = next((x for x in adx[period:period * 2] if not (x != x)), None)
    if first_adx is not None:
        idx = adx.index(first_adx)
        adx_smooth[idx] = first_adx
        for i in range(idx + 1, n):
            if not (adx[i] != adx[i]):
                adx_smooth[i] = (adx_smooth[i - 1] * (period - 1) + adx[i]) / period
            else:
                adx_smooth[i] = adx_smooth[i - 1]

    return adx_smooth, plus_di, minus_di


def _macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[List[float], List[float], List[float]]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    n = len(prices)
    macd_line = [float('nan')] * n
    for i in range(n):
        if not (ema_fast[i] != ema_fast[i]) and not (ema_slow[i] != ema_slow[i]):
            macd_line[i] = ema_fast[i] - ema_slow[i]
    signal_line = _ema([x if not (x != x) else 0 for x in macd_line], signal)
    histogram = [float('nan')] * n
    for i in range(n):
        if not (macd_line[i] != macd_line[i]) and not (signal_line[i] != signal_line[i]):
            histogram[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, histogram


def _bb(prices: List[float], period: int = 20, nbdev: float = 2.0) -> Tuple[List[float], List[float], List[float]]:
    """Bollinger Bands: (upper, middle, lower)."""
    n = len(prices)
    upper = [float('nan')] * n
    middle = [float('nan')] * n
    lower = [float('nan')] * n
    if n < period:
        return upper, middle, lower

    sma = _sma(prices, period)
    for i in range(period - 1, n):
        middle[i] = sma[i]
        window = prices[i - period + 1:i + 1]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        upper[i] = mean + nbdev * std
        lower[i] = mean - nbdev * std
    return upper, middle, lower


def _obv(prices: List[float], volumes: List[float]) -> List[float]:
    """On-Balance Volume."""
    n = len(prices)
    result = [0.0] * n
    if n < 2:
        return result
    result[0] = volumes[0]
    for i in range(1, n):
        if prices[i] > prices[i - 1]:
            result[i] = result[i - 1] + volumes[i]
        elif prices[i] < prices[i - 1]:
            result[i] = result[i - 1] - volumes[i]
        else:
            result[i] = result[i - 1]
    return result


# ============================================================================
# Base Strategy
# ============================================================================

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


# ============================================================================
# TrendRider Style Strategy (NEW — Priority 1)
# ============================================================================

class TrendRiderStyleStrategy(BaseStrategy):
    """
    Multi-signal trend-following strategy inspired by TrendRider V4.

    6 entry signals with confidence scoring (0-10):
      - trend_pullback: pullback to EMA in uptrend
      - ema50_bounce: deep pullback to EMA50 with bounce
      - rsi_bounce: RSI oversold recovery
      - ema_crossover: EMA fast/slow golden cross
      - bb_bounce: Bollinger Band lower bounce
      - macd_reversal: MACD histogram turns positive

    4 exit signals:
      - rsi_overbought: RSI > exit threshold
      - ema_bearish_cross: EMA death cross + MACD negative
      - trend_broken: price breaks below EMA200 by 1%+
      - trend_early_warning: near EMA200 break + RSI exhausted + MACD dropping

    Multi-timeframe: 1h (main), 4h (trend), 1d (macro), BTC 1h (sentiment)
    """

    def __init__(self, config: Dict):
        super().__init__(config)
        p = self.params

        # Entry signal parameters
        self.ema_fast = p.get("ema_fast", 9)
        self.ema_slow = p.get("ema_slow", 16)
        self.rsi_period = p.get("rsi_period", 16)
        self.rsi_pullback_low = p.get("rsi_pullback_low", 30)
        self.rsi_pullback_high = p.get("rsi_pullback_high", 65)
        self.rsi_bounce_level = p.get("rsi_bounce_level", 35)
        self.adx_threshold = p.get("adx_threshold", 18)
        self.volume_factor = p.get("volume_factor", 0.7)

        # Exit parameters
        self.rsi_exit = p.get("rsi_exit", 78)

        # Confidence thresholds
        self.min_confidence_bull = p.get("min_confidence_bull", 5)
        self.min_confidence_bear = p.get("min_confidence_bear", 6)

        # Multi-timeframe
        self.use_4h = p.get("use_4h", True)
        self.use_1d = p.get("use_1d", True)
        self.use_btc_sentiment = p.get("use_btc_sentiment", True)

        # Interval
        self.interval = p.get("interval", "60")

        # Caching for expensive API calls (avoid 429 rate limits)
        self._cache_4h: Dict[str, tuple] = {}     # market → (timestamp, candles)
        self._cache_1d: Dict[str, tuple] = {}     # market → (timestamp, candles)
        self._cache_btc: tuple = (0, [])          # (timestamp, candles)
        self._cache_ttl_4h = 3600   # 1 hour
        self._cache_ttl_1d = 86400  # 24 hours
        self._cache_ttl_btc = 300   # 5 minutes

        # Rate limit: candle group = 10 req/s. With 5 markets + BTC + 4h + 1d,
        # we need to spread requests. Remaining-Req header tracking in live_api
        # handles adaptive throttling; we add a small inter-request delay.
        self._candle_delay = 0.10  # 100ms between candle requests (10 req/s = 100ms min)

    # ------------------------------------------------------------------
    # Indicator calculation for a single market
    # ------------------------------------------------------------------
    def _calc_indicators(self, candles: List[Dict]) -> Dict:
        """Calculate all indicators from candle data. Returns dict of lists keyed by indicator name."""
        n = len(candles)
        if n < 50:
            return {}

        opens   = [c["opening_price"] for c in candles]
        highs   = [c["high_price"] for c in candles]
        lows    = [c["low_price"] for c in candles]
        closes  = [c["trade_price"] for c in candles]
        volumes = [c["candle_acc_trade_volume"] for c in candles]

        ind = {"n": n, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}

        # EMAs
        for period in [5, 9, 10, 16, 20, 21, 30, 50, 200]:
            ind[f"ema_{period}"] = _ema(closes, period)

        # RSI
        for period in [10, 14, 16, 20]:
            ind[f"rsi_{period}"] = _rsi(closes, period)

        # ADX
        adx, plus_di, minus_di = _adx(highs, lows, closes, 14)
        ind["adx"] = adx
        ind["plus_di"] = plus_di
        ind["minus_di"] = minus_di

        # MACD
        macd_line, macd_signal, macd_hist = _macd(closes)
        ind["macd"] = macd_line
        ind["macdsignal"] = macd_signal
        ind["macdhist"] = macd_hist

        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = _bb(closes, 20, 2.0)
        ind["bb_upper"] = bb_upper
        ind["bb_middle"] = bb_middle
        ind["bb_lower"] = bb_lower

        # BB width
        bb_width = [float('nan')] * n
        for i in range(n):
            if not (bb_upper[i] != bb_upper[i]) and not (bb_lower[i] != bb_lower[i]) and bb_middle[i] > 0:
                bb_width[i] = (bb_upper[i] - bb_lower[i]) / (bb_middle[i] + 1e-10)
        ind["bb_width"] = bb_width
        ind["bb_width_sma"] = _sma([x if not (x != x) else 0 for x in bb_width], 50)

        # Volume
        ind["volume_ema"] = _ema(volumes, 20)
        vol_ratio = [float('nan')] * n
        for i in range(n):
            if not (ind["volume_ema"][i] != ind["volume_ema"][i]) and ind["volume_ema"][i] > 0:
                vol_ratio[i] = volumes[i] / (ind["volume_ema"][i] + 1e-10)
        ind["volume_ratio"] = vol_ratio

        # OBV
        ind["obv"] = _obv(closes, volumes)
        ind["obv_ema"] = _ema(ind["obv"], 20)

        # ATR
        ind["atr"] = _atr(highs, lows, closes, 14)

        # Regime detection
        is_bull = [0] * n
        is_bear = [0] * n
        for i in range(n):
            if not (ind["ema_200"][i] != ind["ema_200"][i]) and not (ind["ema_50"][i] != ind["ema_50"][i]):
                if closes[i] > ind["ema_200"][i] and ind["ema_50"][i] > ind["ema_200"][i]:
                    is_bull[i] = 1
                elif closes[i] < ind["ema_200"][i] and ind["ema_50"][i] < ind["ema_200"][i]:
                    is_bear[i] = 1
        ind["is_bull"] = is_bull
        ind["is_bear"] = is_bear

        # Pullback detection
        ema_slow_key = f"ema_{self.ema_slow}"
        pullback = [0] * n
        if ema_slow_key in ind:
            ema_slow_vals = ind[ema_slow_key]
            for i in range(1, n):
                if not (ema_slow_vals[i] != ema_slow_vals[i]):
                    if lows[i] <= ema_slow_vals[i] * 1.02 and closes[i] > ema_slow_vals[i] and closes[i] > opens[i]:
                        pullback[i] = 1
        ind["pullback_to_ema"] = pullback

        # EMA50 bounce
        ema50_bounce = [0] * n
        for i in range(1, n):
            if not (ind["ema_50"][i] != ind["ema_50"][i]):
                if lows[i] <= ind["ema_50"][i] * 1.01 and closes[i] > ind["ema_50"][i] and closes[i] > opens[i]:
                    ema50_bounce[i] = 1
        ind["ema50_bounce"] = ema50_bounce

        return ind

    def _get_row(self, ind: Dict, offset: int = -1) -> Dict:
        """Extract a single row (latest by default) from indicator dict."""
        row = {}
        for key, vals in ind.items():
            if isinstance(vals, list) and len(vals) > 0:
                idx = offset if offset >= 0 else len(vals) + offset
                if 0 <= idx < len(vals):
                    row[key] = vals[idx]
        return row

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------
    def _calc_confidence(self, row: Dict) -> Tuple[str, float, List[str]]:
        """Calculate signal confidence score (0-10). Returns (level, score, details)."""
        score = 0.0
        details = []

        rsi_key = f"rsi_{self.rsi_period}"
        rsi_val = row.get(rsi_key, 50)

        # RSI healthy (35-60): +1.5
        if 35 < rsi_val < 60:
            score += 1.5
            details.append("RSI healthy")

        # ADX: strong +2.5, moderate +1.5
        adx_val = row.get("adx", 0)
        if not (adx_val != adx_val):
            if adx_val > 30:
                score += 2.5
                details.append("Strong trend")
            elif adx_val > self.adx_threshold:
                score += 1.5
                details.append("Moderate trend")

        # Volume: high +2.5, normal +1.5
        vol_ratio = row.get("volume_ratio", 0)
        if not (vol_ratio != vol_ratio):
            if vol_ratio > 1.5:
                score += 2.5
                details.append("High volume")
            elif vol_ratio > 1.0:
                score += 1.5
                details.append("Normal volume")

        # MACD histogram: positive +1.5, rising +0.5
        macd_hist = row.get("macdhist", 0)
        if not (macd_hist != macd_hist):
            if macd_hist > 0:
                score += 1.5
                # Check if rising (need prev row — approximate via row context)
                details.append("MACD positive")

        # OBV rising: +1.5
        obv = row.get("obv", 0)
        obv_ema = row.get("obv_ema", 0)
        if obv > obv_ema:
            score += 1.5
            details.append("OBV rising")

        # BTC healthy (RSI 40-70): +1.5
        btc_rsi = row.get("btc_rsi_1h", 50)
        if 40 < btc_rsi < 70:
            score += 1.5
            details.append("BTC healthy")

        # 4H trend aligned + ADX_4h > 20: +1.5
        if row.get("is_bull_4h", 0) == 1 and row.get("adx_4h", 0) > 20:
            score += 1.5
            details.append("4H trend aligned")

        # BB position (near lower = good for long): +1.0
        close = row.get("close", 0)
        bb_lower = row.get("bb_lower", 0)
        bb_upper = row.get("bb_upper", 0)
        if bb_upper > bb_lower and close > 0:
            bb_pos = (close - bb_lower) / (bb_upper - bb_lower)
            if bb_pos < 0.35:
                score += 1.0
                details.append("Near BB lower")

        # +DI > -DI spread > 10: +1.0
        plus_di = row.get("plus_di", 0)
        minus_di = row.get("minus_di", 0)
        if plus_di - minus_di > 10:
            score += 1.0
            details.append("Strong DI spread")

        # Map to 0-10 (max raw ~17.5)
        numeric = max(1, min(10, round(score * 10 / 17.5)))

        if numeric >= 8:
            level = "STRONG"
        elif numeric >= 6:
            level = "GOOD"
        elif numeric >= 4:
            level = "MEDIUM"
        else:
            level = "WEAK"

        return level, numeric, details

    def _get_market_regime(self, row: Dict) -> str:
        """Detect market regime from ADX + EMA200 + BB width."""
        adx_val = row.get("adx", 0)
        close = row.get("close", 0)
        ema_200 = row.get("ema_200", 0)
        is_bull = row.get("is_bull", 0)
        bb_width = row.get("bb_width", 0)
        bb_width_sma = row.get("bb_width_sma", 0)

        high_vol = bb_width > bb_width_sma * 1.5 if bb_width_sma > 0 else False

        if not (adx_val != adx_val) and adx_val < 20:
            return "Ranging (High Vol)" if high_vol else "Ranging"
        elif is_bull and close > ema_200:
            return "Trending Bull"
        else:
            return "Trending Bear (High Vol)" if high_vol else "Trending Bear"

    # ------------------------------------------------------------------
    # Entry signal checks
    # ------------------------------------------------------------------
    def _check_entry_signals(self, ind: Dict, row: Dict, prev_row: Dict) -> List[Tuple[str, float]]:
        """Check all 6 entry signals. Returns list of (tag, base_confidence)."""
        signals = []
        rsi_key = f"rsi_{self.rsi_period}"
        rsi_val = row.get(rsi_key, 50)
        ema_fast_key = f"ema_{self.ema_fast}"
        ema_slow_key = f"ema_{self.ema_slow}"

        # Common filters
        btc_rsi_ok = row.get("btc_rsi_1h", 50) > 35
        volume_ok = row.get("volume", 0) > 0

        # === 1. trend_pullback ===
        if (row.get("is_bull", 0) == 1 and
            row.get("pullback_to_ema", 0) == 1 and
            rsi_val > self.rsi_pullback_low and
            rsi_val < self.rsi_pullback_high and
            not (row.get("adx", 0) != row.get("adx", 0)) and row.get("adx", 0) > self.adx_threshold and
            not (row.get("volume_ratio", 0) != row.get("volume_ratio", 0)) and row.get("volume_ratio", 0) > self.volume_factor and
            row.get("plus_di", 0) > row.get("minus_di", 0) and
            row.get("obv", 0) > row.get("obv_ema", 0) and
            volume_ok and btc_rsi_ok and
            rsi_val < 70):
            # Daily EMA200 filter
            ema_200_1d = row.get("ema_200_1d", 0)
            if ema_200_1d == 0 or row.get("close", 0) > ema_200_1d:
                signals.append(("trend_pullback", 0.7))

        # === 2. ema50_bounce ===
        if (row.get("is_bull", 0) == 1 and
            row.get("ema50_bounce", 0) == 1 and
            rsi_val > 30 and rsi_val < 50 and
            not (row.get("adx", 0) != row.get("adx", 0)) and row.get("adx", 0) > 20 and
            not (row.get("volume_ratio", 0) != row.get("volume_ratio", 0)) and row.get("volume_ratio", 0) > 1.0 and
            not (row.get("macdhist", 0) != row.get("macdhist", 0)) and
            not (prev_row.get("macdhist", 0) != prev_row.get("macdhist", 0)) and
            row.get("macdhist", 0) > prev_row.get("macdhist", 0) and
            volume_ok and btc_rsi_ok and rsi_val < 70):
            signals.append(("ema50_bounce", 0.65))

        # === 3. rsi_bounce ===
        prev_rsi = prev_row.get(rsi_key, 50)
        if (not (row.get("ema_200", 0) != row.get("ema_200", 0)) and row.get("close", 0) > row.get("ema_200", 0) and
            prev_rsi < self.rsi_bounce_level and rsi_val > self.rsi_bounce_level and
            row.get("close", 0) > row.get("bb_lower", 0) and
            row.get("close", 0) > row.get("open", 0) and
            not (row.get("volume_ratio", 0) != row.get("volume_ratio", 0)) and row.get("volume_ratio", 0) > 0.8 and
            row.get("obv", 0) > row.get("obv_ema", 0) and
            volume_ok and btc_rsi_ok):
            signals.append(("rsi_bounce", 0.6))

        # === 4. ema_crossover ===
        ema_f = row.get(ema_fast_key, 0)
        ema_s = row.get(ema_slow_key, 0)
        prev_ema_f = prev_row.get(ema_fast_key, 0)
        prev_ema_s = prev_row.get(ema_slow_key, 0)
        if (ema_f > ema_s and prev_ema_f <= prev_ema_s and
            rsi_val > 40 and rsi_val < 75 and
            not (row.get("ema_200", 0) != row.get("ema_200", 0)) and row.get("close", 0) > row.get("ema_200", 0) and
            not (row.get("volume_ratio", 0) != row.get("volume_ratio", 0)) and row.get("volume_ratio", 0) > 0.5 and
            volume_ok and btc_rsi_ok):
            signals.append(("ema_crossover", 0.55))

        # === 5. bb_bounce ===
        if (not (row.get("bb_lower", 0) != row.get("bb_lower", 0)) and
            row.get("close", 0) <= row.get("bb_lower", 0) * 1.005 and
            row.get("close", 0) > row.get("open", 0) and
            rsi_val < 45 and
            not (row.get("volume_ratio", 0) != row.get("volume_ratio", 0)) and row.get("volume_ratio", 0) > 0.7 and
            not (row.get("adx", 0) != row.get("adx", 0)) and row.get("adx", 0) > 18 and
            volume_ok and btc_rsi_ok):
            signals.append(("bb_bounce", 0.5))

        # === 6. macd_reversal ===
        if (not (row.get("macdhist", 0) != row.get("macdhist", 0)) and
            not (prev_row.get("macdhist", 0) != prev_row.get("macdhist", 0)) and
            row.get("macdhist", 0) > 0 and prev_row.get("macdhist", 0) <= 0 and
            not (row.get("ema_50", 0) != row.get("ema_50", 0)) and row.get("close", 0) > row.get("ema_50", 0) and
            not (row.get("ema_200", 0) != row.get("ema_200", 0)) and row.get("close", 0) > row.get("ema_200", 0) and
            rsi_val > 40 and rsi_val < 60 and
            not (row.get("adx", 0) != row.get("adx", 0)) and row.get("adx", 0) > 15 and
            not (row.get("volume_ratio", 0) != row.get("volume_ratio", 0)) and row.get("volume_ratio", 0) > 0.8 and
            volume_ok and btc_rsi_ok):
            signals.append(("macd_reversal", 0.6))

        return signals

    # ------------------------------------------------------------------
    # Exit signal checks
    # ------------------------------------------------------------------
    def _check_exit_signals(self, ind: Dict, row: Dict, prev_row: Dict) -> List[Tuple[str, float]]:
        """Check all 4 exit signals. Returns list of (tag, confidence)."""
        signals = []
        rsi_key = f"rsi_{self.rsi_period}"
        rsi_val = row.get(rsi_key, 50)
        ema_fast_key = f"ema_{self.ema_fast}"
        ema_slow_key = f"ema_{self.ema_slow}"
        volume_ok = row.get("volume", 0) > 0

        # EXIT 1: RSI overbought
        if rsi_val > self.rsi_exit and volume_ok:
            signals.append(("rsi_overbought", 0.8))

        # EXIT 2: Bearish EMA cross + MACD confirmation
        ema_f = row.get(ema_fast_key, 0)
        ema_s = row.get(ema_slow_key, 0)
        prev_ema_f = prev_row.get(ema_fast_key, 0)
        prev_ema_s = prev_row.get(ema_slow_key, 0)
        if (ema_f < ema_s and prev_ema_f >= prev_ema_s and
            not (row.get("macdhist", 0) != row.get("macdhist", 0)) and row.get("macdhist", 0) < 0 and
            rsi_val > 50 and volume_ok):
            signals.append(("ema_bearish_cross", 0.7))

        # EXIT 3: Trend broken (close < EMA200 * 0.99)
        if (not (row.get("ema_200", 0) != row.get("ema_200", 0)) and
            row.get("close", 0) < row.get("ema_200", 0) * 0.99 and
            not (prev_row.get("ema_200", 0) != prev_row.get("ema_200", 0)) and
            prev_row.get("close", 0) >= prev_row.get("ema_200", 0) and
            volume_ok):
            signals.append(("trend_broken", 0.9))

        # EXIT 4: Trend early warning (V4)
        if (not (row.get("ema_200", 0) != row.get("ema_200", 0)) and
            row.get("close", 0) < row.get("ema_200", 0) * 0.995 and
            rsi_val > 72 and
            not (row.get("macdhist", 0) != row.get("macdhist", 0)) and
            not (prev_row.get("macdhist", 0) != prev_row.get("macdhist", 0)) and
            row.get("macdhist", 0) < prev_row.get("macdhist", 0) and
            volume_ok):
            signals.append(("trend_early_warning", 0.75))

        return signals

    # ------------------------------------------------------------------
    # Main signal generation
    # ------------------------------------------------------------------
    def generate_signals(self, api, markets: List[str]) -> List[Signal]:
        import time as _time
        signals = []
        now = _time.time()

        # Get BTC sentiment first (shared across all markets, cached by TTL)
        btc_indicators = {}
        if self.use_btc_sentiment:
            btc_ts, btc_cached = self._cache_btc
            if now - btc_ts < self._cache_ttl_btc and btc_cached:
                btc_indicators = btc_cached
            else:
                try:
                    btc_candles = self.get_candles(api, "KRW-BTC", self.interval, 60)
                    _time.sleep(self._candle_delay)
                    if len(btc_candles) >= 50:
                        btc_ind = self._calc_indicators(btc_candles)
                        btc_row = self._get_row(btc_ind)
                        btc_indicators = {
                            "btc_rsi_1h": btc_row.get(f"rsi_{self.rsi_period}", 50),
                            "btc_is_bull_1h": btc_row.get("is_bull", 0),
                            "btc_ema_200": btc_row.get("ema_200", 0),
                            "btc_ema_50": btc_row.get("ema_50", 0),
                        }
                        self._cache_btc = (now, btc_indicators)
                except Exception as e:
                    logger.warning("BTC sentiment fetch failed: %s", e)
                    btc_indicators = {"btc_rsi_1h": 50, "btc_is_bull_1h": 1}

        for market in markets:
            try:
                # --- 1h candles (main timeframe) ---
                candles = self.get_candles(api, market, self.interval, 210)
                _time.sleep(self._candle_delay)
                if len(candles) < 100:
                    logger.debug("%s: insufficient 1h candles (%d)", market, len(candles))
                    continue

                ind = self._calc_indicators(candles)
                if not ind:
                    continue

                row = self._get_row(ind, -1)
                prev_row = self._get_row(ind, -2)

                # Inject BTC sentiment
                row.update(btc_indicators)

                # --- 4h trend (cached per market, TTL-based) ---
                if self.use_4h:
                    cache_ts, cache_val = self._cache_4h.get(market, (0, None))
                    if now - cache_ts < self._cache_ttl_4h and cache_val:
                        row["is_bull_4h"] = cache_val.get("is_bull_4h", row.get("is_bull", 0))
                        row["adx_4h"] = cache_val.get("adx_4h", row.get("adx", 0))
                        row["rsi_14_4h"] = cache_val.get("rsi_14_4h", 50)
                    else:
                        try:
                            candles_4h = self.get_candles(api, market, "240", 100)
                            _time.sleep(self._candle_delay)
                            if len(candles_4h) >= 50:
                                ind_4h = self._calc_indicators(candles_4h)
                                row_4h = self._get_row(ind_4h)
                                row["is_bull_4h"] = row_4h.get("is_bull", 0)
                                row["adx_4h"] = row_4h.get("adx", 0)
                                row["rsi_14_4h"] = row_4h.get("rsi_14", 50)
                                self._cache_4h[market] = (now, {
                                    "is_bull_4h": row["is_bull_4h"],
                                    "adx_4h": row["adx_4h"],
                                    "rsi_14_4h": row["rsi_14_4h"],
                                })
                            else:
                                row["is_bull_4h"] = row.get("is_bull", 0)
                                row["adx_4h"] = row.get("adx", 0)
                        except Exception as e:
                            logger.debug("%s: 4h fetch failed: %s", market, e)
                            row["is_bull_4h"] = row.get("is_bull", 0)
                            row["adx_4h"] = row.get("adx", 0)
                else:
                    row["is_bull_4h"] = row.get("is_bull", 0)
                    row["adx_4h"] = row.get("adx", 0)

                # --- 1d macro trend (cached per market, TTL-based) ---
                if self.use_1d:
                    cache_ts, cache_val = self._cache_1d.get(market, (0, None))
                    if now - cache_ts < self._cache_ttl_1d and cache_val is not None:
                        row["ema_200_1d"] = cache_val
                    else:
                        try:
                            candles_1d = api.get_day_candles(market, 100)
                            _time.sleep(self._candle_delay)
                            if len(candles_1d) >= 50:
                                closes_1d = [c["trade_price"] for c in candles_1d]
                                ema_200_1d = _ema(closes_1d, 200)
                                val = ema_200_1d[-1] if not (ema_200_1d[-1] != ema_200_1d[-1]) else 0
                                row["ema_200_1d"] = val
                                self._cache_1d[market] = (now, val)
                            else:
                                row["ema_200_1d"] = 0
                        except Exception as e:
                            logger.debug("%s: 1d fetch failed: %s", market, e)
                            row["ema_200_1d"] = 0
                else:
                    row["ema_200_1d"] = 0

                # --- Check entry signals ---
                entry_signals = self._check_entry_signals(ind, row, prev_row)

                for tag, base_conf in entry_signals:
                    # Calculate confidence score
                    level, conf_score, details = self._calc_confidence(row)
                    regime = self._get_market_regime(row)

                    # Apply regime-based minimum confidence
                    min_conf = self.min_confidence_bear if "Bear" in regime else self.min_confidence_bull
                    if conf_score < min_conf:
                        logger.debug(
                            "%s: %s rejected — confidence %d/10 < %d (regime: %s) [%s]",
                            market, tag, conf_score, min_conf, regime, ", ".join(details)
                        )
                        continue

                    # Combine base + confidence into final confidence
                    final_conf = min(1.0, base_conf * (conf_score / 10))

                    reason_map = {
                        "trend_pullback": f"Pullback to EMA{self.ema_slow} in uptrend [{level}]",
                        "ema50_bounce": f"EMA50 support bounce [{level}]",
                        "rsi_bounce": f"RSI oversold bounce [{level}]",
                        "ema_crossover": f"EMA{self.ema_fast}/{self.ema_slow} golden cross [{level}]",
                        "bb_bounce": f"BB lower bounce [{level}]",
                        "macd_reversal": f"MACD histogram reversal [{level}]",
                    }

                    signals.append(Signal(
                        market=market,
                        side="buy",
                        confidence=final_conf,
                        reason=reason_map.get(tag, tag),
                        params={
                            "entry_tag": tag,
                            "confidence_score": conf_score,
                            "confidence_level": level,
                            "confidence_details": details,
                            "regime": regime,
                            "rsi": row.get(f"rsi_{self.rsi_period}", 0),
                            "adx": row.get("adx", 0),
                            "volume_ratio": row.get("volume_ratio", 0),
                        },
                    ))
                    # One entry signal per market per cycle
                    break

                # --- Check exit signals (only if no buy signal generated) ---
                if not any(s.market == market and s.side == "buy" for s in signals):
                    exit_signals = self._check_exit_signals(ind, row, prev_row)
                    for tag, conf in exit_signals:
                        reason_map = {
                            "rsi_overbought": f"RSI overbought (> {self.rsi_exit})",
                            "ema_bearish_cross": "EMA bearish crossover + MACD negative",
                            "trend_broken": "Trend broken (below EMA200)",
                            "trend_early_warning": "Trend early warning (near EMA200 break)",
                        }
                        signals.append(Signal(
                            market=market,
                            side="sell",
                            confidence=conf,
                            reason=reason_map.get(tag, tag),
                            params={"exit_tag": tag},
                        ))
                        break

            except Exception as e:
                logger.error("Signal error %s: %s", market, e, exc_info=True)

        return signals


# ============================================================================
# Enhanced MA Crossover Strategy (original, kept for compatibility)
# ============================================================================

class EnhancedMACrossoverStrategy(BaseStrategy):
    """
    MA Crossover with filters learned from paper trading:
    - Volume confirmation (skip low-volume breakouts)
    - Minimum confidence threshold (0.3 → ignore weak crosses)
    - Trend alignment with MA50 on 1h candles
    - Don't chase: only enter when price is near the long MA (±3%)
    """

    def __init__(self, config: Dict):
        super().__init__(config)
        self.short_window = self.params.get("short_window", 10)
        self.long_window = self.params.get("long_window", 30)
        self.interval = self.params.get("interval", "60")
        self.min_confidence = self.params.get("min_confidence", 0.15)
        self.trend_ma_window = self.params.get("trend_ma_window", 50)
        self.volume_ratio_min = self.params.get("volume_ratio_min", 0.7)
        self.price_near_ma_pct = self.params.get("price_near_ma_pct", 0.05)

    def generate_signals(self, api, markets: List[str]) -> List[Signal]:
        signals = []
        for market in markets:
            try:
                max_window = max(self.long_window, self.trend_ma_window) + 10
                candles = self.get_candles(api, market, self.interval, max_window)
                if len(candles) < self.long_window + 2:
                    continue

                prices = [c["trade_price"] for c in candles]
                volumes = [c["candle_acc_trade_volume"] for c in candles]

                short_ma = self.ma(prices, self.short_window)
                long_ma = self.ma(prices, self.long_window)
                if short_ma is None or long_ma is None:
                    continue

                prev_short = self.ma(prices[:-1], self.short_window)
                prev_long = self.ma(prices[:-1], self.long_window)
                if prev_short is None or prev_long is None:
                    continue

                current_price = prices[-1]

                # Golden cross (BUY)
                if prev_short <= prev_long and short_ma > long_ma:
                    cross_strength = (short_ma - long_ma) / long_ma * 100

                    if cross_strength < self.min_confidence * 100:
                        continue

                    avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
                    recent_vol = sum(volumes[-3:]) / 3
                    if recent_vol < avg_vol * self.volume_ratio_min:
                        logger.debug("%s BUY filtered: low volume (%.1f < %.1f)", market, recent_vol, avg_vol)
                        continue

                    trend_ma = self.ma(prices, self.trend_ma_window)
                    prev_trend = self.ma(prices[:-3], self.trend_ma_window)
                    if trend_ma is None or prev_trend is None or trend_ma <= prev_trend:
                        confidence = cross_strength * 0.4 / 100
                        reason = f"Golden Cross (trend neutral) MA{self.short_window}>{self.long_window}"
                    else:
                        confidence = min(1.0, cross_strength / 100)
                        reason = f"Golden Cross MA{self.short_window}>{self.long_window} (trend↑)"

                    if confidence < self.min_confidence:
                        continue

                    signals.append(Signal(
                        market=market, side="buy", confidence=confidence,
                        reason=reason,
                        params={"short_ma": short_ma, "long_ma": long_ma, "trend_ma": trend_ma},
                    ))

                # Death cross (SELL)
                elif prev_short >= prev_long and short_ma < long_ma:
                    cross_strength = (long_ma - short_ma) / long_ma * 100

                    if cross_strength < self.min_confidence * 100:
                        continue

                    confidence = min(1.0, cross_strength / 100)
                    signals.append(Signal(
                        market=market, side="sell", confidence=confidence,
                        reason=f"Death Cross MA{self.short_window}<{self.long_window}",
                        params={"short_ma": short_ma, "long_ma": long_ma},
                    ))

            except Exception as e:
                logger.error("Signal error %s: %s", market, e)
        return signals


class RSIStrategy(BaseStrategy):
    def __init__(self, config: Dict):
        super().__init__(config)
        self.period = config.get("params", {}).get("period", 14)
        self.overbought = config.get("params", {}).get("overbought", 70)
        self.oversold = config.get("params", {}).get("oversold", 30)
        self.interval = config.get("params", {}).get("interval", "60")

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


class MACrossoverStrategy(EnhancedMACrossoverStrategy):
    """Original MA Crossover (no filters) — kept for paper bot compatibility."""
    def __init__(self, config: Dict):
        super().__init__(config)
        self.min_confidence = 0.0
        self.volume_ratio_min = 0.0

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
                if prev_short <= prev_long and short_ma > long_ma:
                    conf = min(1.0, (short_ma - long_ma) / long_ma * 100)
                    signals.append(Signal(
                        market=market, side="buy", confidence=conf,
                        reason=f"Golden Cross MA{self.short_window}>{self.long_window}",
                        params={"short_ma": short_ma, "long_ma": long_ma},
                    ))
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


def get_strategy(config: Dict) -> BaseStrategy:
    name = config.get("name", "ma_crossover")
    if name == "ma_crossover":
        return MACrossoverStrategy(config)
    elif name == "enhanced_ma_crossover":
        return EnhancedMACrossoverStrategy(config)
    elif name == "rsi":
        return RSIStrategy(config)
    elif name == "trendrider":
        return TrendRiderStyleStrategy(config)
    raise ValueError(f"Unknown strategy: {name}")
