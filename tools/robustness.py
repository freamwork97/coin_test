#!/usr/bin/env python3
"""
Robustness harness: walk-forward folds + parameter sensitivity (in/out-of-sample).
Goal: pick a strategy whose OOS performance is CONSISTENT, not the one with the
best fitted backtest (that's how the current bot died).
"""
import sys, argparse, itertools, json
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt


def build(data, unit="day"):
    tl, maps = bt.timeline_of(data)
    closes = {}
    for m, d in data.items():
        arr = [None] * len(tl)
        for j, t in enumerate(d["ts"]):
            arr[maps[m][t]] = d["close"][j]
        closes[m] = arr
    return tl, maps, closes


def walkforward(data, tl, maps, closes, cls, folds=5, maxpos=5, **kw):
    """Anchored walk-forward: train on [0, p_k), test on [p_k, p_{k+1})."""
    n = len(tl)
    out = []
    for k in range(folds):
        a = int(n * (k + 1) / (folds + 2))
        b = int(n * (k + 2) / (folds + 2))
        if b - a < 30:
            continue
        strat = cls()
        pref, sig = strat.run(data, tl, maps)
        m = bt.simulate(tl, closes, pref, sig, max_positions=maxpos,
                        start=a, end=b, **kw)
        out.append((tl[a][:10], tl[b - 1][:10], m))
    return out


def summarize(label, rows):
    rets = [m["total_ret"] for _, _, m in rows]
    mdds = [m["max_dd"] for _, _, m in rows]
    shs = [m["sharpe"] for _, _, m in rows]
    pos = sum(1 for r in rets if r > 0)
    print(f"  {label:38s} folds={len(rows)}  +folds={pos}/{len(rows)}  "
          f"median_ret={sorted(rets)[len(rets)//2]*100:+7.1f}%  "
          f"worst={min(rets)*100:+7.1f}%  best={max(rets)*100:+7.1f}%  "
          f"med_mdd={sorted(mdds)[len(mdds)//2]*100:5.1f}%  med_shp={sorted(shs)[len(shs)//2]:5.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", default="day")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--maxpos", type=int, default=5)
    ap.add_argument("--grid", action="store_true", help="parameter sensitivity sweep")
    args = ap.parse_args()

    data = bt.load(unit=args.unit)
    tl, maps, closes = build(data, args.unit)
    print(f"markets={len(data)} bars={len(tl)} {tl[0][:10]}→{tl[-1][:10]}")

    print("\n=== Walk-forward (anchored) ===")
    print("  [1] Buy&Hold BTC")
    summarize("Buy&Hold BTC", walkforward(data, tl, maps, closes, bt.BuyHold, args.folds, args.maxpos))

    for name, cls, kw in [
        ("DualMomentum default", bt.DualMomentum, {}),
        ("Donchian default", bt.Donchian, {}),
        ("EMA regime default", bt.EmaRegime, {}),
        ("RSI-dip default", bt.RsiDip, {}),
    ]:
        rows = walkforward(data, tl, maps, closes, cls, args.folds, args.maxpos, **kw)
        summarize(name, rows)

    print("\n=== With stop-loss -15% ===")
    for name, cls in [("DualMomentum", bt.DualMomentum), ("Donchian", bt.Donchian),
                      ("EMA regime", bt.EmaRegime)]:
        rows = walkforward(data, tl, maps, closes, cls, args.folds, args.maxpos, stop_loss=-0.15)
        summarize(name + " +SL15", rows)

    if args.grid:
        print("\n=== Donchian parameter grid (walk-forward median) ===")
        for entry, exit_ in itertools.product([20, 40, 55, 80], [10, 20, 30]):
            if exit_ >= entry:
                continue
            cls = lambda e=entry, x=exit_: bt.Donchian(entry=e, exit_=x, ma=200, topk=5)
            rows = walkforward(data, tl, maps, closes, cls, args.folds, args.maxpos)
            rets = [m["total_ret"] for _, _, m in rows]
            pos = sum(1 for r in rets if r > 0)
            med = sorted(rets)[len(rets) // 2]
            print(f"  Donchian({entry:3d}/{exit_:3d})  +folds={pos}/{len(rows)}  median={med*100:+7.1f}%  worst={min(rets)*100:+7.1f}%")

        print("\n=== DualMomentum grid ===")
        for lb, ma in itertools.product([30, 60, 90, 120, 180], [100, 150, 200]):
            cls = lambda l=lb, m=ma: bt.DualMomentum(lookback=l, ma=m, topk=5)
            rows = walkforward(data, tl, maps, closes, cls, args.folds, args.maxpos)
            rets = [m["total_ret"] for _, _, m in rows]
            pos = sum(1 for r in rets if r > 0)
            med = sorted(rets)[len(rets) // 2]
            print(f"  DualMomentum({lb:3d}d,EMA{ma:3d})  +folds={pos}/{len(rows)}  median={med*100:+7.1f}%  worst={min(rets)*100:+7.1f}%")


if __name__ == "__main__":
    main()
