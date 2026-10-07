#!/usr/bin/env python3
"""
Backtest engine + strategies for the Upbit bot (long-only KRW spot, daily bars).

Realism
-------
- fee 0.05% per side + slippage 0.05% per side (Upbit standard, conservative)
- long-only, equal-weight across target positions; cash when flat
- indicators use only CLOSED bars; fills at the same bar's close (no lookahead)
- optional close-based stop-loss / trailing-stop overlays

Strategies compute their state on each market's OWN bars (avoiding None
contamination from markets that listed later), then map onto a global timeline.
"""
from __future__ import annotations
import json, math, os
from typing import Dict, List, Optional

BT_DIR = "/home/windra/.openclaw/workspace/upbit-live-bot/data/backtest"
FEE = 0.0005
SLIP = 0.0005
DAYS = 365


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def ema(v, p):
    out: List[Optional[float]] = [None] * len(v)
    if len(v) < p:
        return out
    k = 2 / (p + 1)
    prev = sum(v[:p]) / p
    out[p - 1] = prev
    for i in range(p, len(v)):
        prev = (v[i] - prev) * k + prev
        out[i] = prev
    return out


def sma(v, p):
    out: List[Optional[float]] = [None] * len(v)
    if len(v) < p:
        return out
    s = float(sum(v[:p]))
    out[p - 1] = s / p
    for i in range(p, len(v)):
        s += v[i] - v[i - p]
        out[i] = s / p
    return out


def rsi(v, p=14):
    out: List[Optional[float]] = [None] * len(v)
    if len(v) < p + 1:
        return out
    g = [max(v[i] - v[i - 1], 0.0) for i in range(1, len(v))]
    l = [max(v[i - 1] - v[i], 0.0) for i in range(1, len(v))]
    ag = sum(g[:p]) / p
    al = sum(l[:p]) / p
    out[p] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(p + 1, len(v)):
        ag = (ag * (p - 1) + g[i - 1]) / p
        al = (al * (p - 1) + l[i - 1]) / p
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load(unit="day", data_dir=BT_DIR):
    data = {}
    if not os.path.isdir(data_dir):
        return data
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(f"_{unit}.json"):
            continue
        m = fn[: -len(f"_{unit}.json")]
        try:
            raw = json.load(open(os.path.join(data_dir, fn)))
        except Exception:
            continue
        if len(raw) < 300:
            continue
        raw = sorted(raw, key=lambda c: c["candle_date_time_utc"])
        data[m] = {
            "ts": [c["candle_date_time_utc"] for c in raw],
            "open": [c["opening_price"] for c in raw],
            "high": [c["high_price"] for c in raw],
            "low": [c["low_price"] for c in raw],
            "close": [c["trade_price"] for c in raw],
        }
    return data


def timeline_of(data):
    allts = set()
    for d in data.values():
        allts.update(d["ts"])
    tl = sorted(allts)
    idx = {t: i for i, t in enumerate(tl)}
    return tl, {m: {t: idx[t] for t in d["ts"]} for m, d in data.items()}


def to_timeline(tl, maps, market, ts, compact):
    out = [False] * len(tl)
    pos = maps[market]
    for j, t in enumerate(ts):
        if compact[j]:
            out[pos[t]] = True
    return out


def emit(tl, maps, data, per):
    return {m: to_timeline(tl, maps, m, data[m]["ts"], c) for m, c in per.items()}


# ---------------------------------------------------------------------------
# Portfolio simulation
# ---------------------------------------------------------------------------
def simulate(tl, closes, pref, signals, initial=1_000_000.0, max_positions=5,
             stop_loss=None, trail_act=None, trail_dist=None, start=None, end=None):
    n = len(tl)
    lo = start if start is not None else 0
    hi = end if end is not None else n
    cash = initial
    pos = {}
    equity = []
    trades = []
    exposure = 0

    for i in range(lo, hi):
        px = lambda m: closes[m][i]

        for m in list(pos.keys()):
            c = px(m)
            if not c:
                continue
            p = pos[m]
            p["peak"] = max(p["peak"], c)
            hit = None
            if stop_loss is not None and (c - p["entry"]) / p["entry"] <= stop_loss:
                hit = "sl"
            elif (trail_act is not None and (p["peak"] - p["entry"]) / p["entry"] >= trail_act
                  and (c - p["peak"]) / p["peak"] <= -(trail_dist or 0)):
                hit = "trail"
            if hit:
                fill = c * (1 - SLIP)
                proceeds = p["qty"] * fill * (1 - FEE)
                cash += proceeds
                trades.append({"market": m, "pnl": proceeds - p["qty"] * p["entry"] * (1 + FEE),
                               "ts": tl[i]})
                del pos[m]

        want = [m for m in pref if signals.get(m, []) and signals[m][i] and px(m)]
        target = set(want[:max_positions])

        for m in list(pos.keys()):
            if m not in target:
                c = px(m)
                fill = c * (1 - SLIP)
                proceeds = pos[m]["qty"] * fill * (1 - FEE)
                cash += proceeds
                trades.append({"market": m, "pnl": proceeds - pos[m]["qty"] * pos[m]["entry"] * (1 + FEE),
                               "ts": tl[i]})
                del pos[m]

        new = [m for m in want if m not in pos][: max(0, max_positions - len(pos))]
        if new:
            pv = sum(pos[m]["qty"] * px(m) for m in pos if px(m))
            eq = cash + pv
            alloc = min(cash * 0.99 / len(new), eq / max_positions)
            for m in new:
                c = px(m)
                if alloc < 5000:
                    break
                fill = c * (1 + SLIP)
                qty = (alloc * (1 - FEE)) / fill
                cash -= alloc
                pos[m] = {"qty": qty, "entry": fill, "peak": fill}
        if pos:
            exposure += 1

        pv = sum(pos[m]["qty"] * px(m) for m in pos if px(m))
        equity.append(cash + pv)

    return _metrics(initial, equity, trades, exposure, hi - lo, tl[lo:hi])


def _metrics(initial, eq, trades, exposure, n, ts):
    if not eq:
        return {}
    final = eq[-1]
    years = max(n / DAYS, 1e-9)
    cagr = (final / initial) ** (1 / years) - 1 if final > 0 else -1
    peak, mdd = -1e18, 0.0
    for e in eq:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    mean = sum(rets) / len(rets) if rets else 0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) if rets else 0
    dn = [r for r in rets if r < 0]
    dsd = math.sqrt(sum(r * r for r in dn) / len(dn)) if dn else 0
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "final": final, "total_ret": final / initial - 1, "cagr": cagr,
        "max_dd": mdd, "sharpe": mean / sd * math.sqrt(DAYS) if sd > 0 else 0,
        "sortino": mean / dsd * math.sqrt(DAYS) if dsd > 0 else 0,
        "trades": len(pnls), "win_rate": len(wins) / len(pnls) if pnls else 0,
        "avg_win": sum(wins) / len(wins) if wins else 0,
        "avg_loss": sum(losses) / len(losses) if losses else 0,
        "expectancy": sum(pnls) / len(pnls) if pnls else 0,
        "exposure": exposure / n if n else 0,
        "calmar": cagr / mdd if mdd > 0 else 0,
        "years": years, "start": ts[0] if ts else "", "end": ts[-1] if ts else "",
    }


def fmt(m):
    if not m:
        return "no data"
    return (f"ret={m['total_ret']*100:+8.1f}% cagr={m['cagr']*100:+7.1f}% "
            f"mdd={m['max_dd']*100:5.1f}% shp={m['sharpe']:5.2f} cal={m['calmar']:5.2f} "
            f"tr={m['trades']:4d} win={m['win_rate']*100:4.1f}% expo={m['exposure']*100:3.0f}%")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
class Strategy:
    name = "base"

    def run(self, data, tl, maps):
        """→ (pref_order, signals_aligned)"""
        raise NotImplementedError


class BuyHold(Strategy):
    def __init__(self, market="KRW-BTC"):
        self.market = market
        self.name = f"Buy&Hold {market}"

    def run(self, data, tl, maps):
        per = {self.market: [True] * len(data[self.market]["ts"])}
        return [self.market], emit(tl, maps, data, per)


class DualMomentum(Strategy):
    def __init__(self, lookback=90, ma=200, topk=5):
        self.lb, self.ma, self.topk = lookback, ma, topk
        self.name = f"DualMomentum({lookback}d,EMA{ma},K{topk})"

    def run(self, data, tl, maps):
        per, momo = {}, {}
        for m, d in data.items():
            c = d["close"]
            maarr = ema(c, self.ma)
            mm: List[Optional[float]] = [None] * len(c)
            a = [False] * len(c)
            for i in range(len(c)):
                if i >= self.lb and c[i - self.lb]:
                    mm[i] = c[i] / c[i - self.lb] - 1
                if mm[i] is not None and mm[i] > 0 and maarr[i] is not None and c[i] > maarr[i]:
                    a[i] = True
            per[m], momo[m] = a, mm
        aligned = {m: [False] * len(tl) for m in data}
        cidx = {m: {t: i for i, t in enumerate(d["ts"])} for m, d in data.items()}
        for t in tl:
            cand = []
            for m, d in data.items():
                j = cidx[m].get(t)
                if j is None or not per[m][j] or momo[m][j] is None:
                    continue
                cand.append((momo[m][j], m))
            cand.sort(reverse=True)
            for _, m in cand[:self.topk]:
                aligned[m][maps[m][t]] = True
        return list(data.keys()), aligned


class Donchian(Strategy):
    def __init__(self, entry=55, exit_=20, ma=200, topk=5):
        self.entry, self.exit_, self.ma, self.topk = entry, exit_, ma, topk
        self.name = f"Donchian({entry}/{exit_},EMA{ma},K{topk})"

    def run(self, data, tl, maps):
        per = {}
        for m, d in data.items():
            c = d["close"]
            maarr = ema(c, self.ma)
            st = [False] * len(c)
            on = False
            for i in range(len(c)):
                hi = c[max(0, i - self.entry):i]
                lo = c[max(0, i - self.exit_):i]
                if not on:
                    if len(hi) >= self.entry and c[i] > max(hi) and maarr[i] is not None and c[i] > maarr[i]:
                        on = True
                else:
                    if len(lo) >= self.exit_ and c[i] < min(lo):
                        on = False
                st[i] = on
            per[m] = st
        return list(data.keys()), emit(tl, maps, data, per)


class EmaRegime(Strategy):
    def __init__(self, fast=50, slow=200, topk=5):
        self.f, self.s, self.topk = fast, slow, topk
        self.name = f"EMA{fast}/{slow} regime (K{topk})"

    def run(self, data, tl, maps):
        per = {}
        for m, d in data.items():
            c = d["close"]
            fa, sa = ema(c, self.f), ema(c, self.s)
            st = [False] * len(c)
            on = False
            for i in range(len(c)):
                if fa[i] is None or sa[i] is None:
                    st[i] = on
                    continue
                if not on and c[i] > fa[i] > sa[i]:
                    on = True
                elif on and c[i] < sa[i]:
                    on = False
                st[i] = on
            per[m] = st
        return list(data.keys()), emit(tl, maps, data, per)


class RsiDip(Strategy):
    def __init__(self, rsi_p=14, buy=35, sell=65, ma=200, topk=5):
        self.rp, self.b, self.s, self.ma, self.topk = rsi_p, buy, sell, ma, topk
        self.name = f"RSI-dip(<{buy},>{sell},EMA{ma})"

    def run(self, data, tl, maps):
        per = {}
        for m, d in data.items():
            c = d["close"]
            ra, maarr = rsi(c, self.rp), ema(c, self.ma)
            st = [False] * len(c)
            on = False
            for i in range(len(c)):
                if ra[i] is None or maarr[i] is None:
                    st[i] = on
                    continue
                if not on and c[i] > maarr[i] and ra[i] < self.b:
                    on = True
                elif on and ra[i] > self.s:
                    on = False
                st[i] = on
            per[m] = st
        return list(data.keys()), emit(tl, maps, data, per)


ALL = {
    "buyhold": BuyHold,
    "dual": DualMomentum,
    "donchian": Donchian,
    "emaregime": EmaRegime,
    "rsidip": RsiDip,
}
