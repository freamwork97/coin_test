#!/usr/bin/env python3
"""Run backtests across strategies with walk-forward splits."""
import sys, argparse
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot/tools")
import backtest as bt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default="")
    ap.add_argument("--unit", default="day")
    ap.add_argument("--maxpos", type=int, default=5)
    ap.add_argument("--stop", type=float, default=None)
    ap.add_argument("--trail-act", type=float, default=None)
    ap.add_argument("--trail-dist", type=float, default=None)
    ap.add_argument("--initial", type=float, default=1_000_000.0)
    args = ap.parse_args()

    data = bt.load(unit=args.unit)
    if args.markets:
        keep = [m.strip() for m in args.markets.split(",") if m.strip()]
        data = {m: d for m, d in data.items() if m in keep}
    if not data:
        print("no cached data"); return
    tl, maps = bt.timeline_of(data)
    closes = {}
    for m, d in data.items():
        arr = [None] * len(tl)
        for j, t in enumerate(d["ts"]):
            arr[maps[m][t]] = d["close"][j]
        closes[m] = arr
    n = len(tl)
    print(f"markets={len(data)}  bars={n}  {tl[0][:10]} → {tl[-1][:10]}")
    print(f"cost: fee {bt.FEE*100:.2f}%/side + slip {bt.SLIP*100:.2f}%/side "
          f"(round trip ~{2*(bt.FEE+bt.SLIP)*100:.2f}%)")
    print(f"engine: maxpos={args.maxpos} stop={args.stop} trail={args.trail_act}/{args.trail_dist}\n")

    split = int(n * 0.6)
    header = (f"{'strategy':34s} {'FULL':>9s} {'IS(60%)':>9s} {'OOS(40%)':>9s}"
              f"  {'mdd':>6s} {'sharpe':>6s} {'tr':>5s} {'win':>5s}")
    print(header); print("-" * len(header))

    for key, cls in bt.ALL.items():
        try:
            strat = cls()
            pref, sig = strat.run(data, tl, maps)
            kw = dict(initial=args.initial, max_positions=args.maxpos,
                      stop_loss=args.stop, trail_act=args.trail_act,
                      trail_dist=args.trail_dist)
            full = bt.simulate(tl, closes, pref, sig, **kw)
            ins = bt.simulate(tl, closes, pref, sig, start=0, end=split, **kw)
            oos = bt.simulate(tl, closes, pref, sig, start=split, end=n, **kw)
            print(f"{strat.name:34s} {full['total_ret']*100:+8.1f}% "
                  f"{ins['total_ret']*100:+8.1f}% {oos['total_ret']*100:+8.1f}%  "
                  f"{full['max_dd']*100:5.1f}% {full['sharpe']:6.2f} "
                  f"{full['trades']:5d} {full['win_rate']*100:4.1f}%")
        except Exception as e:
            import traceback
            print(f"{key:34s}  ERROR: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()


if __name__ == "__main__":
    main()
