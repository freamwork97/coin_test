#!/usr/bin/env python3
"""
V9-C: is the damage from CONCENTRATION (top-3 momentum) or from ALTS generally?

GateBroad(mode):
  'all'     : BTC gate ON → hold ALL majors equal-weight (no ranking, max diversification)
  'btc'     : BTC gate ON → hold BTC only
  'btc+all' : BTC first, then all other majors
"""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize

DATA = bt.load()
MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]


class GateBroad(bt.Strategy):
    def __init__(self, fast=20, slow=50, gate_market="KRW-BTC", mode="all",
                 core="KRW-BTC"):
        self.f, self.s, self.gm, self.mode, self.core = fast, slow, gate_market, mode, core
        self.name = f"GateBroad({mode})"

    def run(self, data, tl, maps):
        g = data[self.gm]
        fa, sa = bt.ema(g["close"], self.f), bt.ema(g["close"], self.s)
        gate = {t: (fa[i] is not None and sa[i] is not None
                    and g["close"][i] > fa[i] > sa[i])
                for i, t in enumerate(g["ts"])}
        aligned = {m: [False] * len(tl) for m in data}
        order = [self.core] + [m for m in data if m != self.core]
        if self.mode == "btc":
            order = [self.core]
        for t in tl:
            if not gate.get(t, False):
                continue
            for m in order:
                if m in data and t in maps[m]:
                    aligned[m][maps[m][t]] = True
        return order, aligned


def prep(markets):
    data = {m: d for m, d in DATA.items() if m in markets}
    return data, *build(data)


def wf(label, cls, maxpos, folds=5, **kw):
    data, tl, maps, closes = prep(MAJORS)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


def period(label, cls, maxpos, last=None, **kw):
    data, tl, maps, closes = prep(MAJORS)
    st = cls()
    pref, sig = st.run(data, tl, maps)
    n = len(tl)
    a = n - last if last else None
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, start=a, end=n, **kw)
    span = f"{tl[a][:10]}→" if a else "full"
    print(f"  {label:38s} [{span:11s}] {bt.fmt(m)}")
    return m


print("=" * 114)
print("1. WALK-FORWARD (5 folds) — diversification vs concentration")
print("=" * 114)
wf("BTC only (maxpos1)", lambda: GateBroad(mode="btc"), 1)
wf("BTC+alt all (maxpos2)", lambda: GateBroad(mode="btc+all"), 2)
wf("BTC+alt all (maxpos4)", lambda: GateBroad(mode="btc+all"), 4)
wf("BTC+alt all (maxpos8)", lambda: GateBroad(mode="btc+all"), 8)
wf("all majors (maxpos8)", lambda: GateBroad(mode="all"), 8)

print("\n" + "=" * 114)
print("2. FULL PERIOD")
print("=" * 114)
period("BTC only", lambda: GateBroad(mode="btc"), 1)
period("BTC+alt all", lambda: GateBroad(mode="btc+all"), 8)
period("all majors", lambda: GateBroad(mode="all"), 8)
period("BTC+alt all + stop-15", lambda: GateBroad(mode="btc+all"), 8, stop_loss=-0.15)
period("BTC+alt all + trail 15/10", lambda: GateBroad(mode="btc+all"), 8,
       trail_act=0.15, trail_dist=0.10)
period("BTC+alt all + stop-15 + portstop12(5d)", lambda: GateBroad(mode="btc+all"), 8,
       stop_loss=-0.15, port_stop=0.12, port_cooldown=5)

print("\n" + "=" * 114)
print("3. RECENT WINDOWS")
print("=" * 114)
for last, tag in [(45, "last45"), (90, "last90"), (180, "last180")]:
    period(f"BTC only {tag}", lambda: GateBroad(mode="btc"), 1, last=last)
    period(f"BTC+alt all {tag}", lambda: GateBroad(mode="btc+all"), 8, last=last)
    print()
