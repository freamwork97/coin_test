#!/usr/bin/env python3
"""V9-F: BTC-heavy weighted allocation — the honest path to a low MDD."""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from v9c import MAJORS, prep
from v9e import GateTrend


def period(label, cls, maxpos, last=None, **kw):
    data, tl, maps, closes = prep(MAJORS)
    st = cls()
    pref, sig = st.run(data, tl, maps)
    n = len(tl)
    a = n - last if last else None
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, start=a, end=n, **kw)
    span = f"{tl[a][:10]}→" if a else "full   "
    print(f"  {label:48s} [{span:11s}] {bt.fmt(m)}")
    return m


def wf(label, cls, maxpos, folds=5, **kw):
    data, tl, maps, closes = prep(MAJORS)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


PROFILES = {
    "BTC50+alt20x2": dict(weights={"KRW-BTC": 0.50}, default_weight=0.20),
    "BTC60+alt15x2": dict(weights={"KRW-BTC": 0.60}, default_weight=0.15),
    "BTC70+alt20x1": dict(weights={"KRW-BTC": 0.70}, default_weight=0.20),
    "BTC40+alt20x3": dict(weights={"KRW-BTC": 0.40}, default_weight=0.20),
}

print("=" * 122)
print("FULL PERIOD — weighted BTC-heavy basket (per-market EMA20>EMA50 filter ON)")
print("=" * 122)
for tag, kw in PROFILES.items():
    mp = 3 if "x2" in tag else (2 if "x1" in tag else 4)
    period(f"{tag} (maxpos{mp})", lambda: GateTrend(mode="all", require_cross=True), mp, **kw)
period("BTC50+alt20x2 + stop-15", lambda: GateTrend(mode="all", require_cross=True), 3,
       **PROFILES["BTC50+alt20x2"], stop_loss=-0.15)
period("BTC50+alt20x2 + stop-15 + cap90", lambda: GateTrend(mode="all", require_cross=True), 3,
       **PROFILES["BTC50+alt20x2"], stop_loss=-0.15, gross_cap=0.90)

print("\n" + "=" * 122)
print("RECENT WINDOWS")
print("=" * 122)
for last, tag in [(45, "last45"), (90, "last90"), (180, "last180")]:
    period(f"BTC50+alt20x2 {tag}", lambda: GateTrend(mode="all", require_cross=True), 3,
           last=last, **PROFILES["BTC50+alt20x2"])
    period(f"BTC70+alt20x1 {tag}", lambda: GateTrend(mode="all", require_cross=True), 2,
           last=last, **PROFILES["BTC70+alt20x1"])
    print()

print("=" * 122)
print("WALK-FORWARD")
print("=" * 122)
for tag, mp in [("BTC50+alt20x2", 3), ("BTC60+alt15x2", 3), ("BTC70+alt20x1", 2)]:
    for nf in [5, 6, 8]:
        wf(f"{tag} folds={nf}", lambda: GateTrend(mode="all", require_cross=True), mp,
           nf, **PROFILES[tag])
    print()
