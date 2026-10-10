"""
하이킨아시 전략 개선 실험 + 워크포워드 검증.

원본(strategies/heikin_ashi_scalp)은 손대지 않고, 여기서:
  1) 타임프레임 확장 (15m/60m/240m 지원)
  2) 추세필터(ADX) 추가
  3) 수수료 편도 0.05% 유지
  4) 워크포워드(순차 구간 분할) 검증

사용법:
    python research/wf.py scan     # 타임프레임 × 시장 × ADX × rr 스윕
    python research/wf.py walk     # 유망 설정 워크포워드
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from heikin_ashi_strategy import FEE_RATE, heikin_ashi  # noqa: E402

UPBIT = "https://api.upbit.com/v1/candles/minutes/{unit}"


# ---------------------------------------------------------------- data
def fetch_candles(market: str, unit: int, total: int, count_per_req: int = 200) -> pd.DataFrame:
    """업비트 분봉 조회. unit=1,3,5,10,15,30,60,240."""
    url = UPBIT.format(unit=unit)
    rows: list[dict] = []
    to = None
    while len(rows) < total:
        params = {"market": market, "count": min(count_per_req, total - len(rows))}
        if to:
            params["to"] = to
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        to = batch[-1]["candle_date_time_utc"]
        time.sleep(0.12)
    df = pd.DataFrame(rows).rename(
        columns={
            "opening_price": "open",
            "high_price": "high",
            "low_price": "low",
            "trade_price": "close",
            "candle_date_time_kst": "time",
        }
    )
    df = df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------- indicators
def add_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ADX (실제 OHLC 기준)."""
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)

    def wilder(s: pd.Series) -> pd.Series:
        return s.ewm(alpha=1 / period, adjust=False).mean()

    atr = wilder(tr)
    plus_di = 100 * wilder(pd.Series(plus_dm, index=df.index)) / atr
    minus_di = 100 * wilder(pd.Series(minus_dm, index=df.index)) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx).rename("adx")


# ---------------------------------------------------------------- engine
def build_signals(df: pd.DataFrame, ema_fast=10, ema_slow=20, adx_min=0.0,
                  touch_tol=0.003, push_pct=0.005, push_lookback=10,
                  swing_n=10, use_goldencross=True, adx_period=14) -> pd.DataFrame:
    d = heikin_ashi(df)
    d["ema_fast"] = d["ha_close"].ewm(span=ema_fast, adjust=False).mean()
    d["ema_slow"] = d["ha_close"].ewm(span=ema_slow, adjust=False).mean()
    d["ha_bull"] = d["ha_close"] > d["ha_open"]
    d["trend_up"] = d["ema_fast"] > d["ema_slow"]
    d["adx"] = add_adx(df, adx_period)

    touched = d["ha_low"] <= d["ema_slow"] * (1 + touch_tol)
    dist_high = (d["ha_high"] - d["ema_slow"]) / d["ema_slow"]
    recent_push = dist_high.shift(1).rolling(push_lookback).max() >= push_pct
    d["sig_pullback"] = (d["trend_up"] & d["ha_bull"] & touched & recent_push).fillna(False)

    crossed = (d["ema_fast"].shift(1) <= d["ema_slow"].shift(1)) & (d["ema_fast"] > d["ema_slow"])
    d["sig_goldencross"] = (crossed & d["ha_bull"]).fillna(False) if use_goldencross else False

    gate = d["adx"] >= adx_min if adx_min > 0 else pd.Series(True, index=d.index)
    d["signal"] = (d["sig_pullback"] | d["sig_goldencross"]) & gate.fillna(False)
    d["swing_low"] = d["low"].shift(1).rolling(swing_n).min()
    return d


def run_backtest(d: pd.DataFrame, risk_pct=0.01, rr_target=1.5,
                 initial_equity=10_000_000) -> tuple[pd.DataFrame, dict]:
    """build_signals()가 만든 df로 백테스트. 원본 엔진과 동일 규칙."""
    n = len(d)
    equity = initial_equity
    trades: list[dict] = []
    pos = None
    brk = 0
    o = d["open"].to_numpy(); h = d["high"].to_numpy()
    lo = d["low"].to_numpy(); c = d["close"].to_numpy()
    es = d["ema_slow"].to_numpy(); ef = d["ema_fast"].to_numpy()
    sig = d["signal"].to_numpy(); spb = d["sig_pullback"].to_numpy()
    swl = d["swing_low"].to_numpy()

    def close_pos(px, i, reason):
        nonlocal equity
        u = pos["units"]
        pnl = u * px * (1 - FEE_RATE) - u * pos["entry"] * (1 + FEE_RATE)
        r = (px - pos["entry"] - pos["entry"] * FEE_RATE - px * FEE_RATE) / (pos["entry"] - pos["stop"])
        equity += pnl
        trades.append({"kind": pos["kind"], "entry": pos["entry"], "exit": px,
                       "reason": reason, "pnl": pnl, "r_multiple": r})

    for i in range(50, n - 1):
        if pos is None:
            if bool(sig[i]):
                entry = float(o[i + 1]); stop = float(swl[i])
                risk = entry - stop
                if risk <= 0 or np.isnan(risk):
                    continue
                units = (equity * risk_pct) / risk
                pos = {"entry": entry, "stop": stop, "target": entry + rr_target * risk,
                       "units": units, "kind": "pullback" if bool(spb[i]) else "goldencross"}
                brk = 0
            continue
        if lo[i] <= pos["stop"]:
            close_pos(pos["stop"], i, "stop"); pos = None; continue
        if h[i] >= pos["target"]:
            close_pos(pos["target"], i, "target"); pos = None; continue
        brk = brk + 1 if c[i] < es[i] else 0
        if brk >= 2 or ef[i] < es[i]:
            close_pos(c[i], i, "trend_break"); pos = None; continue
    if pos is not None:
        close_pos(float(c[-1]), n - 1, "eod")

    return pd.DataFrame(trades), metrics(pd.DataFrame(trades), initial_equity, equity)


def metrics(t: pd.DataFrame, initial: float, final: float) -> dict:
    if len(t) == 0:
        return {"trades": 0}
    wins = t[t["pnl"] > 0]
    gl = -t[t["pnl"] <= 0]["pnl"].sum()
    return {
        "trades": len(t),
        "win_rate": len(wins) / len(t),
        "avg_r": t["r_multiple"].mean(),
        "profit_factor": (wins["pnl"].sum() / gl) if gl > 0 else float("inf"),
        "total_return": (final - initial) / initial,
        "pullback": int((t["kind"] == "pullback").sum()),
    }


# ---------------------------------------------------------------- scan
def scan():
    markets = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL"]
    units = [(15, 4000), (60, 4000), (240, 3000)]
    adxs = [0.0, 20.0, 25.0]
    rrs = [1.5, 2.5]
    cache: dict = {}
    print(f"{'tf':>4} {'market':9} {'adx':>5} {'rr':>4} {'n':>4} {'pb':>3} {'win%':>6} {'pf':>5} {'ret%':>8}")
    rows = []
    for unit, bars in units:
        for mkt in markets:
            df = cache.setdefault((mkt, unit), fetch_candles(mkt, unit, bars))
            for adx in adxs:
                for rr in rrs:
                    d = build_signals(df, adx_min=adx)
                    t, mt = run_backtest(d, rr_target=rr)
                    if mt["trades"] == 0:
                        continue
                    rows.append((unit, mkt, adx, rr, mt["trades"], mt["pullback"],
                                 mt["win_rate"], mt["profit_factor"], mt["total_return"]))
                    print(f"{unit:>4} {mkt:9} {adx:>5.0f} {rr:>4.1f} {mt['trades']:>4} "
                          f"{mt['pullback']:>3} {mt['win_rate']*100:>6.1f} {mt['profit_factor']:>5.2f} "
                          f"{mt['total_return']*100:>+8.1f}", flush=True)
    pos = [r for r in rows if r[8] > 0]
    print(f"\n양수 조합: {len(pos)}/{len(rows)}")
    pd.DataFrame(rows, columns=["tf", "market", "adx", "rr", "n", "pb", "win", "pf", "ret"]).to_csv(
        "research/scan_results.csv", index=False)


# ---------------------------------------------------------------- walk-forward
PARAM_GRID = [{"adx_min": a, "rr_target": r}
              for a in (0.0, 20.0, 25.0) for r in (1.5, 2.5)]


def buy_hold(df: pd.DataFrame) -> float:
    return (df["close"].iloc[-1] - df["close"].iloc[0]) / df["close"].iloc[0]


def _eval(df: pd.DataFrame, p: dict) -> dict:
    d = build_signals(df, adx_min=p["adx_min"])
    _, mt = run_backtest(d, rr_target=p["rr_target"])
    return mt


def walk(market: str, unit: int = 240, bars: int = 6000, folds: int = 6):
    """순차 워크포워드: 직전 구간에서 고른 파라미터를 다음 구간에 적용."""
    df = fetch_candles(market, unit, bars)
    print(f"{market} {unit}m {len(df)}봉  {df['time'].iloc[0]} ~ {df['time'].iloc[-1]}")
    seg = len(df) // folds
    print(f"{'fold':>4} {'구간시작':16} {'IS-pick':>16} {'OOS n':>6} {'OOS win%':>8} "
          f"{'OOS ret%':>9} {'B&H%':>8}")
    oos_rets, bh_rets = [], []
    for k in range(1, folds):
        is_df = df.iloc[(k - 1) * seg: k * seg].reset_index(drop=True)
        oos_df = df.iloc[k * seg: (k + 1) * seg].reset_index(drop=True)
        best, best_ret = None, -9e9
        for p in PARAM_GRID:
            mt = _eval(is_df, p)
            if mt["trades"] >= 10 and mt["total_return"] > best_ret:
                best, best_ret = p, mt["total_return"]
        if best is None:
            best = {"adx_min": 0.0, "rr_target": 1.5}
        mt = _eval(oos_df, best)
        bh = buy_hold(oos_df)
        oos_rets.append(mt.get("total_return", 0.0))
        bh_rets.append(bh)
        tag = f"adx{best['adx_min']:.0f}/rr{best['rr_target']:.1f}"
        print(f"{k:>4} {oos_df['time'].iloc[0]:16} {tag:>16} {mt.get('trades',0):>6} "
              f"{mt.get('win_rate',0)*100:>8.1f} {mt.get('total_return',0)*100:>+9.1f} "
              f"{bh*100:>+8.1f}")
    if oos_rets:
        eq = 1.0
        for r in oos_rets:
            eq *= (1 + r)
        bhe = 1.0
        for r in bh_rets:
            bhe *= (1 + r)
        print(f"\n워크포워드 누적: 전략 {(eq-1)*100:+.1f}%  vs  B&H {(bhe-1)*100:+.1f}%  "
              f"(플러스 구간 {sum(1 for r in oos_rets if r>0)}/{len(oos_rets)})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "scan":
        scan()
    elif cmd == "walk":
        for m in ["KRW-BTC", "KRW-SOL", "KRW-ETH"]:
            walk(m)
            print("-" * 60, flush=True)
    else:
        print("usage: python research/wf.py [scan|walk]")
