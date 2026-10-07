#!/usr/bin/env python3
"""Focused test: majors-only + cash-regime-gate strategies (defensive)."""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]

data_all = bt.load()
print(f"cached markets: {len(data_all)}")

for label, universe in [("ALL 22", list(data_all.keys())), ("MAJORS 8", MAJORS)]:
    data = {m: d for m, d in data_all.items() if m in universe}
    tl, maps, closes = build(data)
    print(f"\n########## {label} ({len(data)} markets, {len(tl)} bars) ##########")
    rows = walkforward(data, tl, maps, closes, bt.BuyHold, 5, 5)
    summarize("Buy&Hold BTC", rows)
    for name, cls in [("EMA50/200 regime K3", lambda: bt.EmaRegime(fast=50, slow=200, topk=3)),
                      ("EMA20/100 regime K3", lambda: bt.EmaRegime(fast=20, slow=100, topk=3)),
                      ("EMA20/50 regime K3", lambda: bt.EmaRegime(fast=20, slow=50, topk=3)),
                      ("DualMom(60,EMA100)K3", lambda: bt.DualMomentum(lookback=60, ma=100, topk=3)),
                      ("DualMom(90,EMA150)K3", lambda: bt.DualMomentum(lookback=90, ma=150, topk=3)),
                      ("Donchian(20/10,EMA100)K3", lambda: bt.Donchian(entry=20, exit_=10, ma=100, topk=3)),
                      ("Donchian(55/20,EMA100)K3", lambda: bt.Donchian(entry=55, exit_=20, ma=100, topk=3))]:
        r = walkforward(data, tl, maps, closes, cls, 5, 5, stop_loss=-0.20)
        summarize(name, r)

# BTC-only regime gate (classic)
print("\n########## BTC-only regime gate ##########")
data = {"KRW-BTC": data_all["KRW-BTC"]}
tl, maps, closes = build(data)
for fast, slow in [(20, 50), (50, 200), (20, 100), (10, 50)]:
    cls = lambda f=fast, s=slow: bt.EmaRegime(fast=f, slow=s, topk=1)
    r = walkforward(data, tl, maps, closes, cls, 5, 1)
    summarize(f"BTC EMA{fast}/{slow}", r)
summarize("BTC Buy&Hold", walkforward(data, tl, maps, closes, bt.BuyHold, 5, 1))
