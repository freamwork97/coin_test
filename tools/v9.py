#!/usr/bin/env python3
"""
V9 strategy overhaul — candidate design + walk-forward validation.

Diagnosis driving the redesign (live, 2026-10-07→09): the V8.1 book bought the
three biggest 30-day movers (AVAX +45.8%, ADA +19.1%, SOL +12.8%) at ~99%
exposure, then the DAILY BTC EMA gate flipped off two days later and dumped
everything into the crash. -9.7% in 2 days.

Design rules for V9 (each one attacks a diagnosed defect):
  1. NO momentum-top chasing. Order candidates by LEAST extension, and refuse
     to buy anything already far above its own fast EMA (the "cap" lesson).
  2. Enter on a PULLBACK into the fast EMA, not on a breakout/extension.
  3. Regime gate on BTC (fast EMA > slow EMA) → cash otherwise.
  4. Per-position hard stop + portfolio drawdown circuit breaker.
  5. Gross exposure cap so one regime flip cannot take the whole book.
"""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]
DATA = bt.load()


# --------------------------------------------------------------------- V9
class RegimeTrend(bt.Strategy):
    """Per-market trend regime + pullback entry + BTC gate.

    state_on(m, i)  : close > ema_slow and ema_fast > ema_slow  (trend regime)
    enter(m, i)     : state_on AND close within `tol` above ema_fast
                      AND extension (close/ema_fast-1) <= `max_ext`
    exit            : close < ema_slow  (state off)
    pref order      : LEAST extended first (anti-chase), BTC pinned first
    """

    def __init__(self, fast=20, slow=50, tol=0.02, max_ext=0.20,
                 gate_fast=20, gate_slow=50, gate_market="KRW-BTC",
                 gate=True, pin=("KRW-BTC",)):
        self.f, self.s, self.tol, self.me = fast, slow, tol, max_ext
        self.gf, self.gs, self.gm = gate_fast, gate_slow, gate_market
        self.gate, self.pin = gate, pin
        self.name = (f"RegimeTrend(EMA{fast}/{slow},tol{tol:.0%},ext{max_ext:.0%}"
                     f"{',gate' if gate else ',nogate'})")

    def run(self, data, tl, maps):
        g = data.get(self.gm)
        gate = {}
        if g is not None:
            fa, sa = bt.ema(g["close"], self.gf), bt.ema(g["close"], self.gs)
            for i, t in enumerate(g["ts"]):
                gate[t] = (fa[i] is not None and sa[i] is not None
                           and g["close"][i] > fa[i] > sa[i])
        per, ext = {}, {}
        for m, d in data.items():
            c = d["close"]
            ea, eb = bt.ema(c, self.f), bt.ema(c, self.s)
            st = [False] * len(c)
            ex = [None] * len(c)
            on = False
            for i in range(len(c)):
                if ea[i] is None or eb[i] is None:
                    st[i] = on
                    continue
                ex[i] = c[i] / ea[i] - 1
                regime = c[i] > eb[i] and ea[i] > eb[i]
                if on:
                    if c[i] < eb[i]:
                        on = False
                else:
                    if regime and 0 <= ex[i] <= self.tol and ex[i] <= self.me:
                        on = True
                st[i] = on
            per[m], ext[m] = st, ex
        aligned = {m: [False] * len(tl) for m in data}
        cidx = {m: {t: i for i, t in enumerate(d["ts"])} for m, d in data.items()}
        for t in tl:
            if self.gate and g is not None and not gate.get(t, False):
                continue
            cand = []
            for m in data:
                j = cidx[m].get(t)
                if j is None or not per[m][j]:
                    continue
                cand.append((ext[m][j] if ext[m][j] is not None else 9e9, m))
            # least-extended first; pinned markets jump the queue
            cand.sort(key=lambda x: (x[1] not in self.pin, x[0]))
            for _, m in cand:
                aligned[m][maps[m][t]] = True
        return list(data.keys()), aligned


class GatedMomentum(bt.Strategy):
    """Exact V8.1 structure (tools/refine.py), inlined so importing refine.py
    does not execute its test block."""

    def __init__(self, fast=20, slow=50, lookback=30, topk=3, gate_market="KRW-BTC",
                 max_momo=None):
        self.f, self.s, self.lb, self.topk = fast, slow, lookback, topk
        self.gate_market, self.max_momo = gate_market, max_momo
        tag = f",cap{max_momo:.0%}" if max_momo else ""
        self.name = f"GatedMom(BTC{fast}/{slow},{lookback}d,K{topk}{tag})"

    def run(self, data, tl, maps):
        g = data[self.gate_market]
        fa, sa = bt.ema(g["close"], self.f), bt.ema(g["close"], self.s)
        gate = {}
        for i, t in enumerate(g["ts"]):
            gate[t] = (fa[i] is not None and sa[i] is not None
                       and g["close"][i] > fa[i] > sa[i])
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
            cand = []
            for m in data:
                j = cidx[m].get(t)
                if j is None or momo[m][j] is None or momo[m][j] <= 0:
                    continue
                if self.max_momo is not None and momo[m][j] > self.max_momo:
                    continue
                cand.append((momo[m][j], m))
            cand.sort(reverse=True)
            for _, m in cand[:self.topk]:
                aligned[m][maps[m][t]] = True
        return list(data.keys()), aligned


def run(label, markets, cls, folds=5, maxpos=3, **kw):
    data = {m: d for m, d in DATA.items() if m in markets}
    tl, maps, closes = build(data)
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


def full(label, markets, cls, maxpos=3, **kw):
    data = {m: d for m, d in DATA.items() if m in markets}
    tl, maps, closes = build(data)
    st = cls()
    pref, sig = st.run(data, tl, maps)
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, **kw)
    print(f"  {label:44s} {bt.fmt(m)}")
    return m


if __name__ == "__main__":
    print("=" * 110)
    print("A. REFERENCES (fresh data through 2026-10-10)")
    print("=" * 110)
    run("Buy&Hold BTC", ["KRW-BTC"], lambda: bt.BuyHold(), 5, 1)
    run("BTC EMA20/50 gate (K1)", ["KRW-BTC"], lambda: bt.EmaRegime(20, 50, 1), 5, 1)
    run("GatedMom 30d K3 (V8.1 live)", MAJORS,
        lambda: GatedMomentum(20, 50, 30, 3), 5, 3)
    run("GatedMom 14d K3", MAJORS, lambda: GatedMomentum(20, 50, 14, 3), 5, 3)
    run("GatedMom 30d K3 + momo cap 30%", MAJORS,
        lambda: GatedMomentum(20, 50, 30, 3, max_momo=0.30), 5, 3)

    print("\n" + "=" * 110)
    print("B. V9 CANDIDATES (walk-forward, 5 folds)")
    print("=" * 110)
    for tol, me in itertools.product([0.01, 0.02, 0.04], [0.15, 0.30]):
        run(f"RegimeTrend tol{tol:.0%} ext{me:.0%}", MAJORS,
            lambda t=tol, x=me: RegimeTrend(tol=t, max_ext=x), 5, 3)
    run("RegimeTrend nogate tol2% ext30%", MAJORS,
        lambda: RegimeTrend(tol=0.02, max_ext=0.30, gate=False), 5, 3)

    print("\n" + "=" * 110)
    print("C. RISK OVERLAY (on RegimeTrend tol2%/ext30%, 5 folds)")
    print("=" * 110)
    base = lambda: RegimeTrend(tol=0.02, max_ext=0.30)
    run("baseline", MAJORS, base, 5, 3)
    run("+ stop -15%", MAJORS, base, 5, 3, stop_loss=-0.15)
    run("+ port_stop 12%", MAJORS, base, 5, 3, port_stop=0.12, port_cooldown=15)
    run("+ stop -15% + port_stop 12%", MAJORS, base, 5, 3, stop_loss=-0.15,
        port_stop=0.12, port_cooldown=15)
    run("+ gross_cap 60%", MAJORS, base, 5, 3, gross_cap=0.60)
    run("+ stop -15% + port_stop 12% + gross_cap 60%", MAJORS, base, 5, 3,
        stop_loss=-0.15, port_stop=0.12, port_cooldown=15, gross_cap=0.60)

    print("\n" + "=" * 110)
    print("D. FOLD-COUNT ROBUSTNESS (RegimeTrend tol2%/ext30% + full risk overlay)")
    print("=" * 110)
    kw = dict(stop_loss=-0.15, port_stop=0.12, port_cooldown=15, gross_cap=0.60)
    for nf in [4, 5, 6, 8]:
        run(f"folds={nf}", MAJORS, base, nf, 3, **kw)

    print("\n" + "=" * 110)
    print("E. FULL PERIOD DETAIL")
    print("=" * 110)
    full("Buy&Hold BTC", ["KRW-BTC"], lambda: bt.BuyHold(), 1)
    full("BTC EMA20/50 gate (K1)", ["KRW-BTC"], lambda: bt.EmaRegime(20, 50, 1), 1)
    full("RegimeTrend base", MAJORS, base, 3)
    full("RegimeTrend + full risk overlay", MAJORS, base, 3, **kw)
    full("RegimeTrend + risk overlay (BTC only)", ["KRW-BTC"], base, 1, **kw)
