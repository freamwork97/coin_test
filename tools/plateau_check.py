#!/usr/bin/env python3
"""Confirm lookback plateau is robust across fold counts (guard vs overfit)."""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward
from refine import GatedMomentum

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]
data = {m: d for m, d in bt.load().items() if m in MAJORS}
tl, maps, closes = build(data)

print("lookback × fold-count  →  (+folds, median%, med_mdd%)")
print(f"{'lb':>4s} | " + " | ".join(f"folds={k}" for k in [4, 5, 6, 8]))
print("-" * 70)
for lb in [14, 21, 28, 30, 45, 60]:
    cells = []
    for nf in [4, 5, 6, 8]:
        rows = walkforward(data, tl, maps, closes,
                           lambda l=lb: GatedMomentum(20, 50, l, 3), nf, 3)
        rets = [r[2]["total_ret"] for r in rows]
        mdds = [r[2]["max_dd"] for r in rows]
        pos = sum(1 for r in rets if r > 0)
        med = sorted(rets)[len(rets) // 2]
        mmd = sorted(mdds)[len(mdds) // 2]
        cells.append(f"{pos}/{len(rows)} {med*100:+5.1f}% {mmd*100:4.1f}%")
    print(f"{lb:>4d} | " + " | ".join(cells))

print("\n=== trailing-stop benefit across lookbacks (folds=5) ===")
for lb in [21, 30]:
    for ta, td in [(None, None), (0.15, 0.08)]:
        kw = {} if ta is None else {"trail_act": ta, "trail_dist": td}
        rows = walkforward(data, tl, maps, closes,
                           lambda l=lb: GatedMomentum(20, 50, l, 3), 5, 3, **kw)
        rets = [r[2]["total_ret"] for r in rows]
        pos = sum(1 for r in rets if r > 0)
        med = sorted(rets)[len(rets) // 2]
        tag = "no trail" if ta is None else f"trail {ta:.0%}/{td:.0%}"
        print(f"  lb={lb} {tag:16s} WF {pos}/{len(rows)} median={med*100:+6.1f}%")
