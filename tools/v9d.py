#!/usr/bin/env python3
"""V9-D: tune the (now-fixed) portfolio circuit breaker + risk overlay."""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from v9c import GateBroad, MAJORS, prep


def period(label, cls, maxpos, last=None, **kw):
    data, tl, maps, closes = prep(MAJORS) if maxpos > 1 else prep(["KRW-BTC"])
    st = cls()
    pref, sig = st.run(data, tl, maps)
    n = len(tl)
    a = n - last if last else None
    m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos, start=a, end=n, **kw)
    span = f"{tl[a][:10]}→" if a else "full   "
    print(f"  {label:44s} [{span:11s}] {bt.fmt(m)}")
    return m


def wf(label, cls, maxpos, folds=5, **kw):
    data, tl, maps, closes = prep(MAJORS) if maxpos > 1 else prep(["KRW-BTC"])
    rows = walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)
    summarize(label, rows)
    return rows


def wf_basket(cls, maxpos, folds, **kw):
    data, tl, maps, closes = prep(MAJORS)
    return walkforward(data, tl, maps, closes, cls, folds, maxpos, **kw)


print("=" * 118)
print("1. BREAKER TUNING (GateBroad btc+all, maxpos8, stop-15) — full period")
print("=" * 118)
period("no breaker", lambda: GateBroad(mode="btc+all"), 8, stop_loss=-0.15)
for ps, cd in itertools.product([0.12, 0.15, 0.20], [3, 5, 10]):
    period(f"port_stop {ps:.0%} cd{cd}d", lambda: GateBroad(mode="btc+all"), 8,
           stop_loss=-0.15, port_stop=ps, port_cooldown=cd)

print("\n" + "=" * 118)
print("2. TUNED CANDIDATES — full vs recent windows")
print("=" * 118)
cands = {
    "no breaker + stop-15": dict(stop_loss=-0.15),
    "breaker 15%/5d + stop-15": dict(stop_loss=-0.15, port_stop=0.15, port_cooldown=5),
    "breaker 20%/5d + stop-15": dict(stop_loss=-0.15, port_stop=0.20, port_cooldown=5),
    "breaker 15%/5d + stop-15 + cap80": dict(stop_loss=-0.15, port_stop=0.15,
                                             port_cooldown=5, gross_cap=0.80),
}
for tag, kw in cands.items():
    period(f"{tag} [full]", lambda: GateBroad(mode="btc+all"), 8, **kw)
    period(f"{tag} [last180]", lambda: GateBroad(mode="btc+all"), 8, last=180, **kw)
    print()

print("=" * 118)
print("3. WALK-FORWARD of the tuned candidates")
print("=" * 118)
for tag, kw in cands.items():
    for nf in [5, 6, 8]:
        rows = wf_basket(lambda: GateBroad(mode="btc+all"), 8, nf, **kw)
        summarize(f"{tag} folds={nf}", rows)
    print()

print("=" * 118)
print("4. REFERENCE")
print("=" * 118)
period("BTC only (no breaker)", lambda: GateBroad(mode="btc"), 1)
period("V8.1 GatedMom 30d K3", lambda: __import__("v9").GatedMomentum(20, 50, 30, 3), 3)
