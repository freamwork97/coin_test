#!/usr/bin/env python3
"""Does expanding the universe beyond the 8 majors help the deployed strategy?"""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from refine import GatedMomentum

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]

all_data = bt.load()
alts = [m for m in all_data if m not in MAJORS]
print(f"total cached: {len(all_data)}  majors: {len(MAJORS)}  alts: {len(alts)}")
print(f"alts: {alts}\n")


def ev(label, markets):
    data = {m: d for m, d in all_data.items() if m in markets}
    tl, maps, closes = build(data)
    # full period
    st = GatedMomentum(fast=20, slow=50, lookback=30, topk=3)
    pref, sig = st.run(data, tl, maps)
    m = bt.simulate(tl, closes, pref, sig, max_positions=3)
    # walk-forward
    rows = walkforward(data, tl, maps, closes,
                       lambda: GatedMomentum(fast=20, slow=50, lookback=30, topk=3), 5, 3)
    rets = [r[2]["total_ret"] for r in rows]
    pos = sum(1 for r in rets if r > 0)
    med = sorted(rets)[len(rets) // 2]
    print(f"  {label:28s} full={m['total_ret']*100:+7.1f}% mdd={m['max_dd']*100:5.1f}% "
          f"shp={m['sharpe']:5.2f} | WF +folds={pos}/{len(rows)} median={med*100:+6.1f}%")


print("=== universe comparison (GatedMom BTC20/50 30d K3) ===")
ev("MAJORS 8 (deployed)", MAJORS)
ev("MAJORS+ALTS all 22", list(all_data.keys()))
ev("all 22, K5", list(all_data.keys()))  # note: K5 needs re-run below
ev("MAJORS 8, K5", MAJORS)

# K5 explicit
for label, markets, k in [("all 22 K5", list(all_data.keys()), 5),
                          ("majors 8 K5", MAJORS, 5)]:
    data = {m: d for m, d in all_data.items() if m in markets}
    tl, maps, closes = build(data)
    rows = walkforward(data, tl, maps, closes,
                       lambda kk=k: GatedMomentum(fast=20, slow=50, lookback=30, topk=kk), 5, k)
    rets = [r[2]["total_ret"] for r in rows]
    pos = sum(1 for r in rets if r > 0)
    print(f"  [K5] {label:24s} WF +folds={pos}/{len(rows)} median={sorted(rets)[len(rets)//2]*100:+6.1f}%")
