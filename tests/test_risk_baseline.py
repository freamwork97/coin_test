"""Regression tests for the 2026-09-30 risk-baseline persistence fixes.

The defect: both risk baselines were recomputed from the live portfolio on
every process start, so a restart silently MOVED the limits.

  - `initial_balance` (the denominator of the daily-loss limit) was rewritten
    to the current portfolio value. Observed 2026-09-28 → 2026-09-30:
    ₩492,397 → ₩469,132. The same ₩-15,917 day read as -3.23% under the old
    baseline and -3.39% under the new one — a restart re-scaled the limit.
  - `peak_value` (the MDD reference) was reset to the current value, and the
    deadlock guard in main_live.check() also reset it whenever the bot was
    flat, so MDD could never survive a restart or a flat period.

Both are persisted to runtime/trader_state.json now, and the deadlock guard
releases the buy halt WITHOUT falsifying the peak.

NOTE: every test gets a temp state path so the live runtime is never touched.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_trader
import main_live


class FakeAPI:
    def __init__(self, portfolio_value=1_000_000.0, krw=1_000_000.0):
        self.portfolio_value = portfolio_value
        self.krw = krw

    def get_krw_balance(self):
        return self.krw

    def get_positions_from_exchange(self):
        return {}

    def get_ticker(self, markets):
        return [{"market": m, "trade_price": 100.0} for m in markets]

    def get_average_buy_price(self, market):
        return None

    def get_accounts(self):
        return []


def make_trader(state_file, pv=1_000_000.0):
    """Build a LiveTrader wired to a temp state file and a fake exchange."""
    t = live_trader.LiveTrader.__new__(live_trader.LiveTrader)
    t.api = FakeAPI(portfolio_value=pv, krw=pv)
    t.fee_rate = 0.0005
    t.trades = []
    t.total_fees = 0.0
    t.consecutive_losses = 0
    t.daily_pnl = {}
    t.initial_balance = 0.0
    t.peak_value = 0.0
    t.initial_balance_recorded = False
    t._state_file = state_file
    return t


# --------------------------------------------------------------------------
# Initial balance must survive a restart
# --------------------------------------------------------------------------
def test_initial_balance_is_persisted():
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, "trader_state.json")
        t1 = make_trader(sf, pv=500_000.0)
        t1._init_balance()
        assert t1.initial_balance == 500_000.0
        assert os.path.exists(sf), "the risk baseline must be persisted"
        assert json.load(open(sf))["initial_balance"] == 500_000.0
    print("PASS test_initial_balance_is_persisted")


def test_initial_balance_survives_restart_with_shrunken_portfolio():
    """The regression: a restart after a loss must NOT move the limit."""
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, "trader_state.json")
        t1 = make_trader(sf, pv=500_000.0)
        t1._init_balance()

        # Restart with the portfolio down 20%: the baseline must not follow it.
        t2 = make_trader(sf, pv=400_000.0)
        t2._init_balance()
        assert t2.initial_balance == 500_000.0, \
            f"restart moved the basis: {t2.initial_balance}"
        # -20,000 on a 500,000 basis is -4%, not -5%
        assert abs((-20_000 / t2.initial_balance) * 100 - (-4.0)) < 0.01
    print("PASS test_initial_balance_survives_restart_with_shrunken_portfolio")


def test_peak_value_survives_restart():
    """MDD reference must not reset to the current (drawn-down) value."""
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, "trader_state.json")
        t1 = make_trader(sf, pv=600_000.0)
        t1._init_balance()
        assert t1.peak_value == 600_000.0

        t2 = make_trader(sf, pv=480_000.0)     # -20% drawdown across a restart
        t2._init_balance()
        assert t2.peak_value == 600_000.0, \
            f"restart erased the MDD peak: {t2.peak_value}"
    print("PASS test_peak_value_survives_restart")


def test_peak_value_rises_and_persists():
    """A new high must be recorded and survive the next restart."""
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, "trader_state.json")
        t1 = make_trader(sf, pv=500_000.0)
        t1._init_balance()
        t1.record_portfolio_value(650_000.0)     # new all-time high
        assert t1.peak_value == 650_000.0
        t2 = make_trader(sf, pv=630_000.0)
        t2._init_balance()
        assert t2.peak_value == 650_000.0, t2.peak_value
    print("PASS test_peak_value_rises_and_persists")


def test_no_state_file_still_bootstraps():
    """Missing/unreadable state must fall back to the live portfolio, not crash."""
    with tempfile.TemporaryDirectory() as d:
        sf = os.path.join(d, "does_not_exist.json")
        t = make_trader(sf, pv=777_000.0)
        t._init_balance()
        assert t.initial_balance == 777_000.0
        assert t.peak_value == 777_000.0
    print("PASS test_no_state_file_still_bootstraps")


# --------------------------------------------------------------------------
# Deadlock guard must release the halt WITHOUT falsifying the peak
# --------------------------------------------------------------------------
SAFETY_CFG = {
    "live_trading": {}, "safety": {
        "max_daily_loss_rate": -0.03, "max_mdd_rate": -0.05,
        "max_single_coin_weight": 0.25, "max_positions": 7, "cooldown_cycles": 60,
        "forced_sell": {}, "cascading_cut": {}, "trailing_stop": {},
        "bear_market": {"enabled": True, "breadth_upgrade_min": 55, "breadth_downgrade_max": 30,
                        "regimes": {"bull": {}, "neutral": {}, "bear": {}}},
        "market_breadth": {"enabled": True, "ema_period": 50, "interval": "60", "thresholds": [[20, 1]]},
    },
}


def test_deadlock_guard_releases_flat_buys_without_erasing_peak():
    """Flat + MDD exceeded: buys allowed, but the peak stays for reporting."""
    with tempfile.TemporaryDirectory() as d:
        s = main_live.LiveSafetyManager(SAFETY_CFG, meta_file=os.path.join(d, "meta.json"))
        t = make_trader(os.path.join(d, "state.json"), pv=500_000.0)
        t.initial_balance = 600_000.0
        t.peak_value = 600_000.0          # -16.7% drawdown
        res = s.check(t)
        assert res["can_trade"] is True, "a flat bot must not be frozen forever"
        assert t.peak_value == 600_000.0, "the drawdown must not be erased"
        assert res["mdd"] > 0.05, "MDD must still be reported while flat"
    print("PASS test_deadlock_guard_releases_flat_buys_without_erasing_peak")


def test_mdd_still_blocks_buys_when_holding_positions():
    """With a position open, the MDD halt must stay fully in force."""
    with tempfile.TemporaryDirectory() as d:
        s = main_live.LiveSafetyManager(SAFETY_CFG, meta_file=os.path.join(d, "meta.json"))
        t = make_trader(os.path.join(d, "state.json"), pv=500_000.0)
        t.initial_balance = 600_000.0
        t.peak_value = 600_000.0
        t.api.get_positions_from_exchange = lambda: {"KRW-BTC": 1.0}
        t.api.get_ticker = lambda markets: [{"market": m, "trade_price": 100.0} for m in markets]
        t.api.get_average_buy_price = lambda market: 100.0
        res = s.check(t)
        assert res["can_trade"] is False, "holding a position must keep the halt"
        assert any("MDD" in v for v in res["violations"]), res["violations"]
    print("PASS test_mdd_still_blocks_buys_when_holding_positions")


# --------------------------------------------------------------------------
# performance reporting must not compare portfolio value to position cost
# --------------------------------------------------------------------------
def test_total_return_uses_all_time_baseline():
    with tempfile.TemporaryDirectory() as d:
        t = make_trader(os.path.join(d, "state.json"), pv=468_342.0)
        t.initial_balance = 469_132.0
        t.initial_balance_recorded = True
        import performance
        pm = performance.PerformanceMetrics(t)
        r = pm.get_returns()
        expected = (468_342.0 - 469_132.0) / 469_132.0 * 100
        assert abs(r["total_return_pct"] - expected) < 0.01, r
        assert abs(r["total_return_pct"]) < 5, \
            f"nonsense return {r['total_return_pct']}% (was comparing to cost basis)"
    print("PASS test_total_return_uses_all_time_baseline")


if __name__ == "__main__":
    test_initial_balance_is_persisted()
    test_initial_balance_survives_restart_with_shrunken_portfolio()
    test_peak_value_survives_restart()
    test_peak_value_rises_and_persists()
    test_no_state_file_still_bootstraps()
    test_deadlock_guard_releases_flat_buys_without_erasing_peak()
    test_mdd_still_blocks_buys_when_holding_positions()
    test_total_return_uses_all_time_baseline()
    print("\nAll risk-baseline persistence tests passed.")
