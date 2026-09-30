"""Regression tests for the 2026-09-30 live-bot defence fixes. No network access.

Covers the three defects that let the portfolio bleed -18.74% from its peak
while every stop-loss appeared to be 'working':

  1. breadth flicker: alt-market breadth oscillated 15-44% across the 30%
     downgrade threshold, flipping REGIME 200 times (bull<->neutral) and with
     it position sizing / confidence floors / max positions. A perfect BTC
     score (4/4) was being overridden by noise.
     Fix: deadband + hysteresis + BTC-score gate on the breadth modifier.

  2. upbit buy orders never report state='done': 506/506 market buys came back
     as state='cancel' with executed_volume > 0, because a market buy that
     cannot spend its whole KRW budget is 'cancel'ed for the remainder.
     _await_order_done() only accepted 'done', so real fills were recorded as
     confirmed=False and the fill was never validated.
     Fix: accept 'cancel' when it actually executed.

  3. cooldown after a loss did not survive a restart: last_sell_cycle was an
     in-memory counter, so a restart wiped it and the bot re-bought the same
     coin immediately (WLD re-bought 1h after a stop-loss, NEAR 1.2h after).
     Fix: persist wall-clock sell times.

NOTE: every test constructs LiveSafetyManager with an explicit temp `meta_file`.
The default path is the LIVE runtime/positions_meta.json, and a test that
forgets this mutates the running bot's position/cooldown state — the exact trap
the upbit-watchdog skill warns about.
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_trader
import main_live


@contextmanager
def isolated_safety(**overrides):
    """Yields a LiveSafetyManager wired to a throwaway meta file."""
    with tempfile.TemporaryDirectory() as d:
        yield make_safety(meta_file=os.path.join(d, "positions_meta.json"), **overrides)


def make_safety(meta_file=None, **overrides):
    """Build a LiveSafetyManager without touching config files or the network."""
    assert meta_file, "meta_file is required so tests never touch live runtime state"
    cfg = {
        "live_trading": {"fee_rate": 0.0005, "cycle_seconds": 60, "position_size_pct": 0.15},
        "safety": {
            "live_trading_enabled": True,
            "max_daily_loss_rate": -0.03,
            "max_mdd_rate": -0.05,
            "max_single_coin_weight": 0.25,
            "max_positions": 7,
            "cooldown_cycles": 60,
            "forced_sell": {"stop_loss_pct": -4.0, "take_profit_pct": 10.0, "max_hold_hours": 48},
            "cascading_cut": {"enabled": True, "thresholds": [[3, -2.0], [6, -1.0], [12, 0.0], [24, 0.5]]},
            "trailing_stop": {"enabled": True, "activation_pct": 4.0, "distance_pct": 2.5},
            "bear_market": {
                "enabled": True,
                "breadth_upgrade_min": 55,
                "breadth_downgrade_max": 30,
                "regimes": {
                    "bull": {"max_positions": 7, "position_size_pct": 0.15, "min_confidence": 5},
                    "neutral": {"max_positions": 4, "position_size_pct": 0.10, "min_confidence": 6},
                    "bear": {"max_positions": 2, "position_size_pct": 0.05, "min_confidence": 7},
                },
            },
            "market_breadth": {"enabled": True, "ema_period": 50, "interval": "60", "thresholds": [[20, 1]]},
        },
    }
    for path, value in overrides.items():
        node = cfg
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return main_live.LiveSafetyManager(cfg, meta_file=meta_file)


class _FakeTrader:
    def get_positions(self):
        return {}


# --------------------------------------------------------------------------
# Fix 1: breadth deadband / hysteresis / BTC-score gate
# --------------------------------------------------------------------------
def test_breadth_does_not_downgrade_a_perfect_btc_score():
    """BTC 4/4 with weak breadth must NOT starve the bot into 'neutral'."""
    with isolated_safety() as s:
        s._btc_regime = "bull"
        s._regime_score = 4            # perfect BTC macro score
        s._current_breadth = 12.0      # alts bleeding
        for _ in range(10):
            assert s._apply_breadth_modifier("bull") == "bull", \
                "breadth noise must not override a perfect BTC score"
    print("PASS test_breadth_does_not_downgrade_a_perfect_btc_score")


def test_breadth_downgrade_requires_hysteresis():
    """One weak reading is noise; only sustained weakness may downgrade."""
    with isolated_safety() as s:
        s._btc_regime = "neutral"
        s._regime_score = 3            # not perfect -> downgrade allowed
        s._current_breadth = 15.0
        assert s._apply_breadth_modifier("neutral") == "neutral"   # too early
        s._consecutive_weak_breadth = 10
        assert s._apply_breadth_modifier("neutral") == "bear"      # confirmed
    print("PASS test_breadth_downgrade_requires_hysteresis")


def test_breadth_weak_reading_resets_when_breadth_recovers():
    """Recovery must reset the confirm counter so the regime stops flip-flopping."""
    with isolated_safety() as s:
        s._regime_score = 3
        s._current_breadth = 45.0          # above the downgrade band -> recovered
        s._consecutive_weak_breadth = 5
        assert s._apply_breadth_modifier("neutral") == "neutral"
        assert s._consecutive_weak_breadth == 0, "recovery must reset the counter"
        s._current_breadth = 40.0          # mid-band: deadband, no upgrade either
        assert s._apply_breadth_modifier("neutral") == "neutral"
    print("PASS test_breadth_weak_reading_resets_when_breadth_recovers")


def test_sizing_stays_stable_under_breadth_flicker():
    """End-to-end: the real 09-30 breadth sequence must not change sizing."""
    with isolated_safety() as s:
        s._btc_regime = "bull"
        s._regime_score = 4
        sizes = []
        for breadth in (44.0, 17.0, 31.0, 15.0, 42.0, 19.0):
            s._current_breadth = breadth
            s._apply_breadth_modifier(s._btc_regime)
            _, size, _ = s._get_effective_limits()
            sizes.append(size)
        assert len(set(sizes)) == 1, f"sizing flickered with breadth: {sizes}"
    print("PASS test_sizing_stays_stable_under_breadth_flicker")


# --------------------------------------------------------------------------
# Fix 2: state='cancel' buy orders that actually executed
# --------------------------------------------------------------------------
class CancelStateAPI:
    """Mirrors the real Upbit /order payload for a filled market buy."""

    def __init__(self, state, executed_volume):
        self.state = state
        self.executed_volume = executed_volume
        self.polls = 0

    def get_order(self, uuid):
        self.polls += 1
        filled = float(self.executed_volume) > 0
        return {
            "uuid": uuid,
            "state": self.state,
            "executed_volume": self.executed_volume,
            "paid_fee": "36.09",
            "trades_count": 1 if filled else 0,
            "trades": [{"funds": "72182.21"}] if filled else [],
        }


def _bare_trader(api):
    t = live_trader.LiveTrader.__new__(live_trader.LiveTrader)
    t.api = api
    return t


def test_await_order_done_accepts_cancel_with_execution():
    """The real bug: every buy is state='cancel' but funded. Treat as filled."""
    t = _bare_trader(CancelStateAPI("cancel", "1670.88459695"))
    detail = t._await_order_done("uuid", timeout=1.0, interval=0.01)
    assert detail is not None, "a 'cancel' order that executed must be confirmed"
    assert detail["state"] == "cancel"
    assert t.api.polls == 1, "a terminal filled order needs only one lookup"
    print("PASS test_await_order_done_accepts_cancel_with_execution")


def test_await_order_done_rejects_cancel_without_execution():
    """A genuinely unfilled/cancelled order must NOT be reported as filled."""
    t = _bare_trader(CancelStateAPI("cancel", "0"))
    assert t._await_order_done("uuid", timeout=0.2, interval=0.01) is None
    assert t.api.polls == 1, "an empty cancel is terminal — stop polling"
    print("PASS test_await_order_done_rejects_cancel_without_execution")


def test_await_order_done_still_accepts_done():
    t = _bare_trader(CancelStateAPI("done", "1.5"))
    detail = t._await_order_done("uuid", timeout=1.0, interval=0.01)
    assert detail is not None and detail["state"] == "done"
    print("PASS test_await_order_done_still_accepts_done")


def test_await_order_done_polls_while_wait_state():
    """A 'wait' order must keep being polled until it settles."""
    api = CancelStateAPI("wait", "1.5")
    t = _bare_trader(api)
    # 'wait' never settles -> timeout returns None, but it must have polled >1
    assert t._await_order_done("uuid", timeout=0.1, interval=0.01) is None
    assert api.polls > 1, f"expected repeated polling, got {api.polls}"
    print("PASS test_await_order_done_polls_while_wait_state")


# --------------------------------------------------------------------------
# Fix 3: cooldown must survive a restart
# --------------------------------------------------------------------------
def test_cooldown_survives_restart():
    with tempfile.TemporaryDirectory() as d:
        meta = os.path.join(d, "positions_meta.json")
        s1 = make_safety(meta_file=meta)
        s1.on_sell("KRW-WLD")
        assert os.path.exists(meta), "sell time must be persisted"
        saved = json.load(open(meta))
        assert "KRW-WLD" in saved.get("cooldowns", {}), saved

        s2 = make_safety(meta_file=meta)      # simulated restart
        s2._load_position_meta()
        assert s2.can_buy("KRW-WLD", _FakeTrader()) is False, \
            "restart must not clear the cooldown after a loss"
    print("PASS test_cooldown_survives_restart")


def test_cooldown_expires_after_window():
    with tempfile.TemporaryDirectory() as d:
        meta = os.path.join(d, "positions_meta.json")
        s1 = make_safety(meta_file=meta)
        s1.on_sell("KRW-WLD")
        m = json.load(open(meta))
        m["cooldowns"]["KRW-WLD"] -= (s1.cooldown_cycles * 60) + 1
        with open(meta, "w") as f:
            json.dump(m, f)
        s2 = make_safety(meta_file=meta)
        s2._load_position_meta()
        assert s2.can_buy("KRW-WLD", _FakeTrader()) is True
    print("PASS test_cooldown_expires_after_window")


def test_config_cooldown_is_an_hour():
    """config says cooldown_cycles=60; after the unit fix that means 60 minutes."""
    with isolated_safety() as s:
        s.on_sell("KRW-WLD")
        s.last_sell_time["KRW-WLD"] -= 59 * 60
        assert s.can_buy("KRW-WLD", _FakeTrader()) is False
        s.last_sell_time["KRW-WLD"] -= 2 * 60
        assert s.can_buy("KRW-WLD", _FakeTrader()) is True
    print("PASS test_config_cooldown_is_an_hour")


def test_meta_load_ignores_cooldowns_key_as_position():
    """The persisted 'cooldowns' key must never be adopted as a position."""
    with tempfile.TemporaryDirectory() as d:
        meta = os.path.join(d, "positions_meta.json")
        s1 = make_safety(meta_file=meta)
        s1.on_sell("KRW-WLD")
        s2 = make_safety(meta_file=meta)
        s2._load_position_meta()
        assert "cooldowns" not in s2.position_entry_time
        assert "cooldowns" not in s2.position_peak_price
        assert s2.last_sell_time.get("KRW-WLD") is not None
    print("PASS test_meta_load_ignores_cooldowns_key_as_position")


if __name__ == "__main__":
    test_breadth_does_not_downgrade_a_perfect_btc_score()
    test_breadth_downgrade_requires_hysteresis()
    test_breadth_weak_reading_resets_when_breadth_recovers()
    test_sizing_stays_stable_under_breadth_flicker()
    test_await_order_done_accepts_cancel_with_execution()
    test_await_order_done_rejects_cancel_without_execution()
    test_await_order_done_still_accepts_done()
    test_await_order_done_polls_while_wait_state()
    test_cooldown_survives_restart()
    test_cooldown_expires_after_window()
    test_config_cooldown_is_an_hour()
    test_meta_load_ignores_cooldowns_key_as_position()
    print("\nAll defence-fix regression tests passed.")
