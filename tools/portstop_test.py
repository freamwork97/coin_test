#!/usr/bin/env python3
"""Test portfolio drawdown circuit breaker — directly targets growing losses."""
import sys
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt
from robustness import build, walkforward
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
    mmd = sorted(mdds)[len(mdds) // 2]
    st = cls()
    pref, sig = st.run(data, tl, maps)
    fm = bt.simulate(tl, closes, pref, sig, max_positions=k, **simkw)
    print(f"  {label:32s} full={fm['total_ret']*100:+7.1f}% mdd={fm['max_dd']*100:5.1f}% "
          f"shp={fm['sharpe']:5.2f} | WF {pos}/{len(rows)} med={med*100:+6.1f}% medmdd={mmd*100:5.1f}%")


print("=== baseline (no portfolio stop) ===")
wf("current", lambda: GatedMomentum(20, 50, 30, 3))

print("\n=== portfolio drawdown circuit breaker (cool=10 bars) ===")
for ps in [0.10, 0.15, 0.20, 0.25]:
    wf(f"port_stop {ps:.0%}", lambda: GatedMomentum(20, 50, 30, 3),
       port_stop=ps, port_cooldown=10)

print("\n=== port_stop 15% with varying cooldown ===")
for cd in [5, 10, 20, 30]:
    wf(f"port_stop 15% cool={cd}", lambda: GatedMomentum(20, 50, 30, 3),
       port_stop=0.15, port_cooldown=cd)

print("\n=== combination: port_stop 15% + lookback 21 ===")
wf("port15 + lb21", lambda: GatedMomentum(20, 50, 21, 3), port_stop=0.15, port_cooldown=10)
wf("port15 + lb21 K2", lambda: GatedMomentum(20, 50, 21, 2), k=2, port_stop=0.15, port_cooldown=10)
