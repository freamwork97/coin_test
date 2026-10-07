#!/usr/bin/env python3
"""Verify the -15% portfolio stop is robust (fold counts + alt universe)."""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward
from refine import GatedMomentum

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]
all_data = bt.load()

for label, markets in [("MAJORS 8", MAJORS), ("ALL 22", list(all_data.keys()))]:
    data = {m: d for m, d in all_data.items() if m in markets}
    tl, maps, closes = build(data)
    print(f"\n### {label} ###")
    for ps in [None, 0.15, 0.20]:
        line = []
        for nf in [4, 5, 6, 8]:
            kw = {} if ps is None else {"port_stop": ps, "port_cooldown": 10}
            rows = walkforward(data, tl, maps, closes,
                               lambda: GatedMomentum(20, 50, 30, 3), nf, 3, **kw)
            rets = [r[2]["total_ret"] for r in rows]
            mdds = [r[2]["max_dd"] for r in rows]
            pos = sum(1 for r in rets if r > 0)
            line.append(f"f{nf}:{pos}/{len(rows)} mdd{sorted(mdds)[len(mdds)//2]*100:.0f}%")
        tag = "no stop" if ps is None else f"stop {ps:.0%}"
        print(f"  {tag:9s} " + " | ".join(line))
