#!/usr/bin/env python3
"""Fix attempt: entry-quality filters + faster exits, judged by walk-forward."""
import sys, itertools
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward, summarize
from refine import GatedMomentum

MAJORS = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
          "KRW-AVAX", "KRW-LINK"]
data = {m: d for m, d in bt.load().items() if m in MAJORS}
tl, maps, closes = build(data)


def wf(label, cls, k=3, folds=5, **simkw):
    rows = walkforward(data, tl, maps, closes, cls, folds, k, **simkw)
    rets = [r[2]["total_ret"] for r in rows]
    mdds = [r[2]["max_dd"] for r in rows]
    pos = sum(1 for r in rets if r > 0)
    med = sorted(rets)[len(rets) // 2]
    med_mdd = sorted(mdds)[len(mdds) // 2]
    # full period
    st = cls()
    pref, sig = st.run(data, tl, maps)
    fm = bt.simulate(tl, closes, pref, sig, max_positions=k, **simkw)
    print(f"  {label:34s} full={fm['total_ret']*100:+7.1f}% mdd={fm['max_dd']*100:5.1f}% "
          f"shp={fm['sharpe']:5.2f} tr={fm['trades']:3d} | WF {pos}/{len(rows)} med={med*100:+6.1f}% medmdd={med_mdd*100:5.1f}%")


print("=== baseline (deployed) ===")
wf("GatedMom 30d K3 (current)", lambda: GatedMomentum(20, 50, 30, 3))

print("\n=== A) momentum cap (don't chase blow-off tops) ===")
for cap in [0.20, 0.30, 0.40, 0.60]:
    wf(f"cap {cap:.0%}", lambda c=cap: GatedMomentum(20, 50, 30, 3, max_momo=c))

print("\n=== B) shorter momentum lookback (fresher) ===")
for lb in [7, 14, 21]:
    wf(f"lookback {lb}d", lambda l=lb: GatedMomentum(20, 50, l, 3))

print("\n=== C) fewer positions (K2) ===")
wf("K2", lambda: GatedMomentum(20, 50, 30, 2))
wf("K2 cap40", lambda: GatedMomentum(20, 50, 30, 2, max_momo=0.40))

print("\n=== D) exit overlays ===")
for stop in [-0.08, -0.12, -0.15]:
    wf(f"+SL {stop:.0%}", lambda: GatedMomentum(20, 50, 30, 3), stop_loss=stop)
for ta, td in [(0.10, 0.05), (0.15, 0.08), (0.20, 0.10)]:
    wf(f"+trail act{ta:.0%}/dist{td:.0%}", lambda: GatedMomentum(20, 50, 30, 3),
       trail_act=ta, trail_dist=td)

print("\n=== E) faster gate (react sooner) ===")
for f, s in [(10, 20), (10, 30), (15, 30)]:
    wf(f"gate {f}/{s}", lambda ff=f, ss=s: GatedMomentum(ff, ss, 30, 3))
