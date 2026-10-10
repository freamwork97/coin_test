#!/usr/bin/env python3
"""V9 candidate selection: plateau sweeps, fold robustness, recent-window
behaviour (would it have dodged the 2026-10-08 crash?), cost sensitivity."""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from v9 import GatedMomentum, RegimeTrend  # noqa: F401  (module-level DATA load)

DATA = bt.load()
MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]


def prep(markets):
    data = {m: d for m, d in DATA.items() if m in markets}
    return data, *build(data)


def wf(label, cls, markets=MAJORS, folds=5, maxpos=3, **kw):
    data, tl, maps, closes = prep(markets)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


def period(label, cls, markets=MAJORS, maxpos=3, last=None, **kw):
    data, tl, maps, closes = prep(markets)
    st = cls()
    pref, sig = st.run(data, tl, maps)
    n = len(tl)
    a = n - last if last else None
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, start=a, end=n, **kw)
    span = f"{tl[a][:10]}→{tl[-1][:10]}" if a else "full"
    print(f"  {label:40s} [{span}] {bt.fmt(m)}")
    return m


print("=" * 112)
print("1. MOMENTUM LOOKBACK PLATEAU (K3, maxpos 3, walk-forward 5 folds)")
print("=" * 112)
for lb in [10, 14, 18, 25, 30, 45]:
    wf(f"GatedMom {lb:2d}d K3", lambda l=lb: GatedMomentum(20, 50, l, 3))

print("\n" + "=" * 112)
print("2. TOP-K PLATEAU (14d lookback, walk-forward 5 folds)")
print("=" * 112)
for k in [2, 3, 4]:
    wf(f"GatedMom 14d K{k}", lambda kk=k: GatedMomentum(20, 50, 14, kk))
wf("BTC EMA20/50 gate (K1, BTC only)", lambda: bt.EmaRegime(20, 50, 1),
   markets=["KRW-BTC"], maxpos=1)

print("\n" + "=" * 112)
print("3. FOLD-COUNT ROBUSTNESS (GatedMom 14d K3)")
print("=" * 112)
for nf in [4, 5, 6, 8]:
    wf(f"14d K3 folds={nf}", lambda: GatedMomentum(20, 50, 14, 3), folds=nf)

print("\n" + "=" * 112)
print("4. RECENT WINDOW — would it have dodged the 10-08 crash?")
print("=" * 112)
for last, tag in [(45, "last 45d"), (90, "last 90d")]:
    period("V8.1 GatedMom 30d K3", lambda: GatedMomentum(20, 50, 30, 3), last=last)
    period("GatedMom 14d K3", lambda: GatedMomentum(20, 50, 14, 3), last=last)
    period("BTC EMA20/50 gate", lambda: bt.EmaRegime(20, 50, 1),
           markets=["KRW-BTC"], maxpos=1, last=last)
    period("BTC Buy&Hold", lambda: bt.BuyHold(), markets=["KRW-BTC"], maxpos=1,
           last=last)
    print()

print("=" * 112)
print("5. FULL PERIOD DETAIL")
print("=" * 112)
period("V8.1 GatedMom 30d K3", lambda: GatedMomentum(20, 50, 30, 3))
period("GatedMom 14d K2", lambda: GatedMomentum(20, 50, 14, 2))
period("GatedMom 14d K3", lambda: GatedMomentum(20, 50, 14, 3))
period("GatedMom 14d K3 + gross_cap 60%", lambda: GatedMomentum(20, 50, 14, 3),
       gross_cap=0.60)
period("GatedMom 14d K3 + port_stop 12%", lambda: GatedMomentum(20, 50, 14, 3),
       port_stop=0.12, port_cooldown=15)
period("GatedMom 14d K3 + stop-15 + portstop12 + cap60",
       lambda: GatedMomentum(20, 50, 14, 3), stop_loss=-0.15,
       port_stop=0.12, port_cooldown=15, gross_cap=0.60)
period("BTC EMA20/50 gate", lambda: bt.EmaRegime(20, 50, 1),
       markets=["KRW-BTC"], maxpos=1)
period("BTC Buy&Hold", lambda: bt.BuyHold(), markets=["KRW-BTC"], maxpos=1)

print("\n" + "=" * 112)
print("6. COST SENSITIVITY (fee+slippage 0.1%/side — 2x conservative)")
print("=" * 112)
bt.FEE, bt.SLIP = 0.001, 0.001
period("GatedMom 14d K3", lambda: GatedMomentum(20, 50, 14, 3))
period("BTC EMA20/50 gate", lambda: bt.EmaRegime(20, 50, 1),
       markets=["KRW-BTC"], maxpos=1)
bt.FEE, bt.SLIP = 0.0005, 0.0005
