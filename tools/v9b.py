#!/usr/bin/env python3
"""
V9-B: BTC-anchored core + limited satellite.

Rationale: the live loss came from a 99%-alt book. Cap the damage structurally:
  * BTC always takes the first slot when the regime gate is ON (core).
  * At most (maxpos-1) alts join, chosen by lookback momentum, positive only,
    and never already extended beyond `max_momo` (anti-chase).
  * Gate OFF → cash.
With maxpos=2 the book is BTC + 1 alt (≈50/50) instead of 3 alts at 99%.
"""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize

DATA = bt.load()
MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]


class GatedCoreSat(bt.Strategy):
    def __init__(self, fast=20, slow=50, lookback=14, topk=3, max_momo=0.30,
                 gate_market="KRW-BTC", core="KRW-BTC", sat_ok=True):
        self.f, self.s, self.lb, self.topk = fast, slow, lookback, topk
        self.max_momo, self.gm, self.core, self.sat_ok = max_momo, gate_market, core, sat_ok
        self.name = (f"CoreSat(BTC{fast}/{slow},{lookback}d,K{topk},cap{max_momo:.0%}"
                     f"{',sat' if sat_ok else ',coreonly'})")

    def run(self, data, tl, maps):
        g = data[self.gm]
        fa, sa = bt.ema(g["close"], self.f), bt.ema(g["close"], self.s)
        gate = {t: (fa[i] is not None and sa[i] is not None
                    and g["close"][i] > fa[i] > sa[i])
                for i, t in enumerate(g["ts"])}
        momo = {}
        for m, d in data.items():
            c = d["close"]
            mm = [None] * len(c)
            for i in range(len(c)):
                if i >= self.lb and c[i - self.lb]:
                    mm[i] = c[i] / c[i - self.lb] - 1
            momo[m] = mm
        cidx = {m: {t: i for i, t in enumerate(d["ts"])} for m, d in data.items()}
        aligned = {m: [False] * len(tl) for m in data}
        for t in tl:
            if not gate.get(t, False):
                continue
            j = cidx[self.core].get(t)
            if j is not None:                      # core always on when gate is on
                aligned[self.core][maps[self.core][t]] = True
            if not self.sat_ok:
                continue
            cand = []
            for m in data:
                if m == self.core:
                    continue
                j = cidx[m].get(t)
                if j is None or momo[m][j] is None:
                    continue
                if momo[m][j] <= 0 or momo[m][j] > self.max_momo:
                    continue
                cand.append((momo[m][j], m))
            cand.sort(reverse=True)
            for _, m in cand[: self.topk]:
                aligned[m][maps[m][t]] = True
        return list(data.keys()), aligned


def prep(markets):
    data = {m: d for m, d in DATA.items() if m in markets}
    return data, *build(data)


def wf(label, cls, maxpos, folds=5, **kw):
    data, tl, maps, closes = prep(MAJORS)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


def period(label, cls, maxpos, last=None, **kw):
    data, tl, maps, closes = prep(MAJORS if maxpos > 1 else ["KRW-BTC"])
    st = cls()
    pref, sig = st.run(data, tl, maps)
    n = len(tl)
    a = n - last if last else None
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, start=a, end=n, **kw)
    span = f"{tl[a][:10]}→" if a else "full"
    print(f"  {label:42s} [{span:11s}] {bt.fmt(m)}")
    return m


print("=" * 116)
print("1. WALK-FORWARD (5 folds)")
print("=" * 116)
wf("BTC gate only (maxpos1)", lambda: bt.EmaRegime(20, 50, 1), 1)
for mp, lb, k in [(2, 14, 3), (2, 30, 3), (3, 14, 3), (3, 30, 3), (2, 14, 2), (3, 14, 5)]:
    wf(f"CoreSat maxpos{mp} {lb}d K{k}", 
       lambda l=lb, kk=k: GatedCoreSat(lookback=l, topk=kk), mp)
wf("CoreSat 14d K3 maxpos2 + cap60", lambda: GatedCoreSat(lookback=14, topk=3), 2,
   gross_cap=0.60)
wf("CoreSat 14d K3 maxpos2 + stop-15", lambda: GatedCoreSat(lookback=14, topk=3), 2,
   stop_loss=-0.15)

print("\n" + "=" * 116)
print("2. FULL PERIOD")
print("=" * 116)
period("BTC gate only", lambda: bt.EmaRegime(20, 50, 1), 1)
period("CoreSat 14d K3 maxpos2", lambda: GatedCoreSat(lookback=14, topk=3), 2)
period("CoreSat 14d K3 maxpos3", lambda: GatedCoreSat(lookback=14, topk=3), 3)
period("CoreSat 14d K3 maxpos2 + stop-15", lambda: GatedCoreSat(lookback=14, topk=3), 2,
       stop_loss=-0.15)
period("CoreSat 14d K3 maxpos2 + cap60", lambda: GatedCoreSat(lookback=14, topk=3), 2,
       gross_cap=0.60)

print("\n" + "=" * 116)
print("3. RECENT WINDOWS (the live-failure regime)")
print("=" * 116)
for last, tag in [(45, "last45"), (90, "last90")]:
    period(f"BTC gate only {tag}", lambda: bt.EmaRegime(20, 50, 1), 1, last=last)
    period(f"CoreSat 14d K3 maxpos2 {tag}", lambda: GatedCoreSat(lookback=14, topk=3),
           2, last=last)
    period(f"V8.1 30d K3 maxpos3 {tag}",
           lambda: __import__("v9").GatedMomentum(20, 50, 30, 3), 3, last=last)
    print()

print("=" * 116)
print("4. TRADE LIST around the crash (CoreSat 14d K3, maxpos2, last 45d)")
print("=" * 116)
data, tl, maps, closes = prep(MAJORS)
st = GatedCoreSat(lookback=14, topk=3)
pref, sig = st.run(data, tl, maps)
n = len(tl)
m = bt.simulate(tl, closes, pref, sig, max_positions=2, start=n - 45, end=n)
print(f"  {bt.fmt(m)}")
