#!/usr/bin/env python3
"""V9-G: profiles the LIVE engine can express (equal weight, maxpos 2/3)."""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from v9c import GateBroad, MAJORS, prep
from v9e import GateTrend


def period(label, cls, maxpos, last=None, **kw):
    data, tl, maps, closes = prep(MAJORS)
    st = cls()
    pref, sig = st.run(data, tl, maps)
    n = len(tl)
    a = n - last if last else None
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, start=a, end=n, **kw)
    span = f"{tl[a][:10]}→" if a else "full   "
    print(f"  {label:46s} [{span:11s}] {bt.fmt(m)}")
    return m


def wf(label, cls, maxpos, folds, **kw):
    data, tl, maps, closes = prep(MAJORS)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)


CAND = {
    "Basket all maxpos2 (equal)": (lambda: GateBroad(mode="btc+all"), 2, {}),
    "Basket all maxpos2 + mktfilter": (lambda: GateTrend(mode="all", require_cross=True), 2, {}),
    "Basket all maxpos2 + mktfilter + stop15": (
        lambda: GateTrend(mode="all", require_cross=True), 2, dict(stop_loss=-0.15)),
    "Basket all maxpos3 + mktfilter": (lambda: GateTrend(mode="all", require_cross=True), 3, {}),
    "core+sat K1 maxpos2 + mktfilter + stop15": (
        lambda: GateTrend(mode="sat", topk=1, require_cross=True), 2, dict(stop_loss=-0.15)),
}

print("=" * 118)
print("FULL PERIOD")
print("=" * 118)
for tag, (cls, mp, kw) in CAND.items():
    period(tag, cls, mp, **kw)

print("\n" + "=" * 118)
print("RECENT (last180 / last90 / last45)")
print("=" * 118)
for tag, (cls, mp, kw) in CAND.items():
    for last in (180, 90, 45):
        period(f"{tag} [{last}d]", cls, mp, last=last, **kw)
    print()

print("=" * 118)
print("WALK-FORWARD 5/6/8")
print("=" * 118)
for tag, (cls, mp, kw) in CAND.items():
    for nf in (5, 6, 8):
        wf(f"{tag} f{nf}", cls, mp, nf, **kw)
    print()
