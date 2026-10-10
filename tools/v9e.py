#!/usr/bin/env python3
"""
V9-E: per-market trend filter — the real fix for the 10-08 style loss.

The 27.9% MDD of the broad basket comes from holding alts that are individually
broken while the BTC gate is still ON. Add a per-market regime test:
    hold market m only if  close_m > EMA50_m  (and optionally EMA20_m > EMA50_m)
"""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from v9c import MAJORS, prep


class GateTrend(bt.Strategy):
    """BTC gate AND per-market trend. mode='all' | 'core+sat'."""

    def __init__(self, gate_fast=20, gate_slow=50, f=20, s=50, gate_market="KRW-BTC",
                 mode="all", core="KRW-BTC", lookback=14, topk=3, max_momo=0.30,
                 require_cross=False):
        self.gf, self.gs, self.f, self.s = gate_fast, gate_slow, f, s
        self.gm, self.mode, self.core = gate_market, mode, core
        self.lb, self.topk, self.mm = lookback, topk, max_momo
        self.require_cross = require_cross
        self.name = f"GateTrend({mode},EMA{f}/{s},cross={require_cross})"

    def run(self, data, tl, maps):
        g = data[self.gm]
        ga, gb = bt.ema(g["close"], self.gf), bt.ema(g["close"], self.gs)
        gate = {t: (ga[i] is not None and gb[i] is not None
                    and g["close"][i] > ga[i] > gb[i])
                for i, t in enumerate(g["ts"])}
        per, momo = {}, {}
        for m, d in data.items():
            c = d["close"]
            fa, sa = bt.ema(c, self.f), bt.ema(c, self.s)
            on = [False] * len(c)
            mm = [None] * len(c)
            for i in range(len(c)):
                if i >= self.lb and c[i - self.lb]:
                    mm[i] = c[i] / c[i - self.lb] - 1
                if fa[i] is None or sa[i] is None:
                    continue
                ok = c[i] > sa[i]
                if self.require_cross:
                    ok = ok and fa[i] > sa[i]
                on[i] = ok
            per[m], momo[m] = on, mm
        cidx = {m: {t: i for i, t in enumerate(d["ts"])} for m, d in data.items()}
        aligned = {m: [False] * len(tl) for m in data}
        for t in tl:
            if not gate.get(t, False):
                continue
            if self.mode == "all":
                order = [self.core] + [m for m in data if m != self.core]
                for m in order:
                    j = cidx[m].get(t)
                    if j is not None and per[m][j]:
                        aligned[m][maps[m][t]] = True
            else:  # core + satellite
                j = cidx[self.core].get(t)
                if j is not None and per[self.core][j]:
                    aligned[self.core][maps[self.core][t]] = True
                cand = []
                for m in data:
                    if m == self.core:
                        continue
                    j = cidx[m].get(t)
                    if j is None or not per[m][j] or momo[m][j] is None:
                        continue
                    if momo[m][j] <= 0 or momo[m][j] > self.mm:
                        continue
                    cand.append((momo[m][j], m))
                cand.sort(reverse=True)
                for _, m in cand[: self.topk]:
                    aligned[m][maps[m][t]] = True
        return [self.core] + [m for m in data if m != self.core], aligned


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


def wf(label, cls, maxpos, folds=5, **kw):
    data, tl, maps, closes = prep(MAJORS)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


print("=" * 120)
print("FULL PERIOD")
print("=" * 120)
period("B reference (btc+all, no per-mkt filter)", lambda: __import__("v9c").GateBroad(mode="btc+all"), 8)
period("GateTrend all, close>EMA50", lambda: GateTrend(mode="all", require_cross=False), 8)
period("GateTrend all, EMA20>EMA50", lambda: GateTrend(mode="all", require_cross=True), 8)
period("GateTrend all EMA20>50 + stop-15", lambda: GateTrend(mode="all", require_cross=True), 8,
       stop_loss=-0.15)
period("GateTrend core+sat K3 14d", lambda: GateTrend(mode="sat"), 4)
period("GateTrend core+sat K3 14d EMA20>50", lambda: GateTrend(mode="sat", require_cross=True), 4)

print("\n" + "=" * 120)
print("RECENT WINDOWS (live-failure regime)")
print("=" * 120)
for last, tag in [(45, "last45"), (90, "last90"), (180, "last180")]:
    period(f"B reference (btc+all) {tag}",
           lambda: __import__("v9c").GateBroad(mode="btc+all"), 8, last=last)
    period(f"GateTrend all EMA20>50 {tag}",
           lambda: GateTrend(mode="all", require_cross=True), 8, last=last)
    print()

print("=" * 120)
print("WALK-FORWARD")
print("=" * 120)
for nf in [5, 6, 8]:
    wf(f"GateTrend all EMA20>50 folds={nf}",
       lambda: GateTrend(mode="all", require_cross=True), 8, nf)
for nf in [5, 6, 8]:
    wf(f"GateTrend core+sat K3 folds={nf}",
       lambda: GateTrend(mode="sat", require_cross=True), 4, nf)
