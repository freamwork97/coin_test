#!/usr/bin/env python3
"""Final candidate validation: gate robustness + full-period detail + cost sensitivity."""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from refine import GatedMomentum

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]
data_all = bt.load()
D = {m: d for m, d in data_all.items() if m in MAJORS}
tl, maps, closes = build(D)


def full(cls, label, maxpos=3):
    st = cls()
    pref, sig = st.run(D, tl, maps)
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos)
    print(f"  {label:42s} {bt.fmt(m)}")
    return m


print("=== FULL PERIOD (2024-07-30 → 2026-10-07, majors 8) ===")
full(lambda: bt.BuyHold(), "Buy&Hold BTC", 1)
full(lambda: bt.EmaRegime(fast=20, slow=50, topk=1), "BTC EMA20/50 gate only", 1)
full(lambda: GatedMomentum(fast=20, slow=50, lookback=30, topk=3), "GatedMom BTC20/50 30d K3", 3)
full(lambda: GatedMomentum(fast=20, slow=50, lookback=30, topk=2), "GatedMom BTC20/50 30d K2", 2)
full(lambda: GatedMomentum(fast=20, slow=50, lookback=60, topk=3), "GatedMom BTC20/50 60d K3", 3)
full(lambda: GatedMomentum(fast=20, slow=50, lookback=14, topk=3), "GatedMom BTC20/50 14d K3", 3)

print("\n=== gate-parameter robustness (full period, GatedMom 30d K3) ===")
for f, s in [(10, 30), (10, 50), (15, 40), (20, 50), (20, 60), (20, 100), (30, 60), (30, 100), (50, 100)]:
    try:
        m = full(lambda f=f, s=s: GatedMomentum(fast=f, slow=s, lookback=30, topk=3), f"gate {f}/{s}", 3)
    except Exception as e:
        print(f"  gate {f}/{s}: ERROR {e}")

print("\n=== walk-forward summary (folds=5) ===")
for label, cls, k in [
    ("Buy&Hold BTC", bt.BuyHold, 1),
    ("BTC EMA20/50 gate", lambda: bt.EmaRegime(fast=20, slow=50, topk=1), 1),
    ("GatedMom 30d K3", lambda: GatedMomentum(fast=20, slow=50, lookback=30, topk=3), 3),
    ("GatedMom 60d K3", lambda: GatedMomentum(fast=20, slow=50, lookback=60, topk=3), 3),
]:
    summarize(label, walkforward(D, tl, maps, closes, cls, 5, k))
