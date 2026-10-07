#!/usr/bin/env python3
"""
Refinement: BTC-regime gate + momentum rotation.
  - Regime: BTC fast-EMA above slow-EMA  → risk-ON (alts allowed), else CASH
  - In risk-ON: hold top-K momentum majors
This is the only structure with consistently positive walk-forward results.
Test fold-count robustness too.
"""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]


class GatedMomentum(bt.Strategy):
    """BTC regime gate + top-K momentum among universe.
    gate: BTC close > EMA_fast > EMA_slow (risk-on). Else cash (no positions).

    max_momo: optional entry cap — skip a market whose lookback return exceeds
    this (guards against chasing a blow-off top; e.g. AVAX bought at +46%).
    """
    def __init__(self, fast=20, slow=50, lookback=30, topk=3, gate_market="KRW-BTC",
                 max_momo=None):
        self.f, self.s, self.lb, self.topk = fast, slow, lookback, topk
        self.gate_market = gate_market
        self.max_momo = max_momo
        tag = f",cap{max_momo:.0%}" if max_momo else ""
        self.name = f"GatedMom(BTC{fast}/{slow}, {lookback}d, K{topk}{tag})"

    def run(self, data, tl, maps):
        g = data[self.gate_market]
        fa, sa = bt.ema(g["close"], self.f), bt.ema(g["close"], self.s)
        gate = {}
        for i, t in enumerate(g["ts"]):
            on = (fa[i] is not None and sa[i] is not None
                  and g["close"][i] > fa[i] > sa[i])
            gate[t] = on
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
                    continue  # too extended → don't chase
                cand.append((momo[m][j], m))
            cand.sort(reverse=True)
            for _, m in cand[:self.topk]:
                aligned[m][maps[m][t]] = True
        return list(data.keys()), aligned


data_all = bt.load()


def test(label, markets, cls):
    data = {m: d for m, d in data_all.items() if m in markets}
    tl, maps, closes = build(data)
    rows = walkforward(data, tl, maps, closes, cls, 5, 5)
    summarize(label, rows)
    return rows


print("### MAJORS 8 ###")
test("BTC20/50 gate alone (K1)", ["KRW-BTC"], lambda: bt.EmaRegime(fast=20, slow=50, topk=1))
for lb, k in itertools.product([14, 30, 60], [2, 3, 5]):
    test(f"GatedMom BTC20/50 {lb}d K{k}", MAJORS,
         lambda l=lb, kk=k: GatedMomentum(fast=20, slow=50, lookback=l, topk=kk))

print("\n### fold-count robustness (BTC20/50 gate alone) ###")
D = {"KRW-BTC": data_all["KRW-BTC"]}
tl, maps, closes = build(D)
for nf in [4, 5, 6, 8]:
    rows = walkforward(D, tl, maps, closes, lambda: bt.EmaRegime(fast=20, slow=50, topk=1), nf, 1)
    summarize(f"folds={nf}", rows)

print("\n### full-period detail: BTC20/50 gate alone ###")
st = bt.EmaRegime(fast=20, slow=50, topk=1)
pref, sig = st.run(D, tl, maps)
m = bt.simulate(tl, closes, pref, sig, max_positions=1)
print("  ", bt.fmt(m))
bh = bt.BuyHold(); p2, s2 = bh.run(D, tl, maps)
print("   Buy&Hold:", bt.fmt(bt.simulate(tl, closes, p2, s2, max_positions=1)))
