"""Regression tests for the 2026-09-28 live-bot fixes. No network access.

Covers:
  1. live_trader: order fill is polled until 'done' (not a single 0.3s lookup),
     and unconfirmed fills skip realized-PnL / daily-PnL updates.
  2. strategy_live: ema_bearish_cross exit fires even when RSI already collapsed.
  3. strategy_live: confidence divisor matches the true max raw score (15.5).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_trader
from strategy_live import TrendRiderStyleStrategy


# --------------------------------------------------------------------------
# Fake exchange API
# --------------------------------------------------------------------------
class FakeAPI:
    def __init__(self):
        self.order_polls = 0
        self.fill_after = 3        # polls required before the order reports 'done'
        self.never_fills = False
        self.executed_volume = "1.5"
        self.executed_funds = "150000"
        self.paid_fee = "75"
        self.avg_buy_price = 90000.0
        self.positions = {"KRW-BTC": 1.5}

    def get_krw_balance(self):
        return 10_000_000

    def get_positions_from_exchange(self):
        return dict(self.positions)

    def get_average_buy_price(self, market):
        return self.avg_buy_price

    def get_ticker(self, markets):
        return [{"market": m, "trade_price": 100000.0} for m in markets]

    def market_sell(self, market, volume):
        return {"uuid": "sell-uuid", "created_at": "2026-09-28T00:00:00"}

    def market_buy(self, market, amount_krw):
        return {"uuid": "buy-uuid", "created_at": "2026-09-28T00:00:00"}

    def get_order(self, uuid):
        self.order_polls += 1
        if not self.never_fills and self.order_polls >= self.fill_after:
            # Mirrors the real /order payload: top-level executed_funds is NOT
            # populated by Upbit; the traded amount only exists in trades[].funds.
            return {"uuid": uuid, "state": "done",
                    "executed_volume": self.executed_volume,
                    "executed_funds": "0",
                    "paid_fee": self.paid_fee,
                    "trades_count": 2,
                    "trades": [{"funds": "90000.0"}, {"funds": "60000.0"}]}
        return {"uuid": uuid, "state": "wait",
                "executed_volume": "0", "executed_funds": "0", "paid_fee": "0"}


def make_trader(tmpdir):
    live_trader.TRADES_FILE = os.path.join(tmpdir, "trades.jsonl")
    live_trader.EQUITY_FILE = os.path.join(tmpdir, "equity.csv")
    live_trader.DAILY_PNL_FILE = os.path.join(tmpdir, "daily_pnl.json")
    t = live_trader.LiveTrader.__new__(live_trader.LiveTrader)
    t.config = {}
    t.api = FakeAPI()
    t.fee_rate = 0.0005
    t.trades = []
    t.total_fees = 0.0
    t.peak_value = 0.0
    t.consecutive_losses = 0
    t.daily_pnl = {}
    t.initial_balance = 10_000_000
    return t


# --------------------------------------------------------------------------
# Fix 1: fill polling
# --------------------------------------------------------------------------
def test_sell_polls_until_done():
    with tempfile.TemporaryDirectory() as d:
        t = make_trader(d)
        trade = t.sell("KRW-BTC")
        assert trade is not None
        assert trade["confirmed"] is True
        # Old code did a single lookup (1 poll); the fix must poll repeatedly.
        assert t.api.order_polls >= 3, f"expected polling, got {t.api.order_polls} lookup(s)"
        # funds must come from trades[].funds (90000+60000), not the empty top-level field
        assert trade["executed_funds"] == "150000.0", trade["executed_funds"]
        # realized = funds - fee - vol * avg_buy = 150000 - 75 - 1.5*90000
        assert trade["realized_pnl"] == 150000 - 75 - 1.5 * 90000
        assert len(t.daily_pnl) == 1  # daily-loss circuit input updated
    print("PASS test_sell_polls_until_done")


def test_extract_execution_uses_trades_funds():
    """Regression: top-level executed_funds is 0 on real Upbit responses."""
    ex = live_trader.LiveTrader._extract_execution({
        "executed_volume": "2.5", "paid_fee": "10", "executed_funds": "0",
        "trades": [{"funds": "1000.5"}, {"funds": "2000.5"}],
    })
    assert ex["funds"] == 3001.0, ex
    assert ex["volume"] == 2.5, ex
    assert ex["fee"] == 10.0, ex
    # no trades[] -> fall back to the legacy field
    ex2 = live_trader.LiveTrader._extract_execution({"executed_funds": "777"})
    assert ex2["funds"] == 777.0, ex2
    assert live_trader.LiveTrader._extract_execution(None)["funds"] == 0.0
    print("PASS test_extract_execution_uses_trades_funds")


def test_sell_unconfirmed_skips_pnl():
    with tempfile.TemporaryDirectory() as d:
        t = make_trader(d)
        t.api.never_fills = True
        t._await_order_done = lambda uuid: None  # simulate timeout
        trade = t.sell("KRW-BTC")
        assert trade is not None
        assert trade["confirmed"] is False
        assert trade["realized_pnl"] is None
        assert t.daily_pnl == {}          # must NOT pollute the circuit breaker
        assert t.consecutive_losses == 0
    print("PASS test_sell_unconfirmed_skips_pnl")


def test_sell_order_failure_returns_none():
    with tempfile.TemporaryDirectory() as d:
        t = make_trader(d)
        t.api.market_sell = lambda market, volume: None
        assert t.sell("KRW-BTC") is None
    print("PASS test_sell_order_failure_returns_none")


# --------------------------------------------------------------------------
# Fix 2: ema_bearish_cross must not require RSI > 50
# --------------------------------------------------------------------------
def test_ema_bearish_cross_fires_when_rsi_collapsed():
    strat = TrendRiderStyleStrategy({"params": {}})
    row = {"rsi_16": 28, "ema_9": 95.0, "ema_16": 100.0,
           "macdhist": -1.0, "volume": 100.0}
    prev_row = {"ema_9": 101.0, "ema_16": 100.0}
    sigs = strat._check_exit_signals({}, row, prev_row)
    tags = [tag for tag, _ in sigs]
    assert "ema_bearish_cross" in tags, f"missing exit, got {tags}"
    print("PASS test_ema_bearish_cross_fires_when_rsi_collapsed")


# --------------------------------------------------------------------------
# Fix 3: confidence divisor == true max raw score (15.5)
# --------------------------------------------------------------------------
def test_confidence_max_score_reaches_strong():
    strat = TrendRiderStyleStrategy({"params": {}})
    row = {
        "rsi_16": 50,          # +1.5  (RSI healthy)
        "adx": 35,             # +2.5  (strong trend)
        "volume_ratio": 2.0,   # +2.5  (high volume)
        "macdhist": 1.0,       # +1.5  (MACD positive)
        "obv": 10, "obv_ema": 5,   # +1.5 (OBV rising)
        "btc_rsi_1h": 55,      # +1.5  (BTC healthy)
        "is_bull_4h": 1, "adx_4h": 25,  # +1.5 (4H aligned)
        "close": 100, "bb_lower": 90, "bb_upper": 130,  # +1.0 (near BB lower)
        "plus_di": 30, "minus_di": 15,  # +1.0 (DI spread)
    }
    level, numeric, details = strat._calc_confidence(row)
    assert len(details) == 9, f"expected all 9 factors, got {details}"
    assert numeric == 10, f"perfect score should map to 10, got {numeric}"
    assert level == "STRONG"
    print("PASS test_confidence_max_score_reaches_strong")


if __name__ == "__main__":
    test_sell_polls_until_done()
    test_extract_execution_uses_trades_funds()
    test_sell_unconfirmed_skips_pnl()
    test_sell_order_failure_returns_none()
    test_ema_bearish_cross_fires_when_rsi_collapsed()
    test_confidence_max_score_reaches_strong()
    print("ALL PASS")
