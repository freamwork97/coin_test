"""
업비트 5분봉 데이터로 하이킨아시 단타 전략 백테스트.

사용법:
    python backtest.py --market KRW-BTC --candles 3000 --risk 0.01 --rr 1.5
"""

from __future__ import annotations

import argparse
import time

import pandas as pd
import requests

from heikin_ashi_strategy import backtest

UPBIT = "https://api.upbit.com/v1/candles/minutes/5"


def fetch_candles(market: str, total: int, count_per_req: int = 200) -> pd.DataFrame:
    """Upbit 분봉 조회 (최신 -> 과거 페이지네이션)."""
    rows: list[dict] = []
    to = None
    while len(rows) < total:
        params = {"market": market, "count": min(count_per_req, total - len(rows))}
        if to:
            params["to"] = to
        r = requests.get(UPBIT, params=params, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        to = batch[-1]["candle_date_time_utc"]
        time.sleep(0.12)  # rate limit 배려
    df = pd.DataFrame(rows)
    df = df.rename(
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KRW-BTC")
    ap.add_argument("--candles", type=int, default=3000, help="5분봉 개수 (3000 = 약 10.4일)")
    ap.add_argument("--risk", type=float, default=0.01, help="1회 리스크 비율")
    ap.add_argument("--rr", type=float, default=1.5, help="익절 손익비 (R 배수)")
    ap.add_argument("--out", default="results", help="결과 저장 폴더")
    args = ap.parse_args()

    print(f"[1/3] {args.market} {args.candles}개 5분봉 조회 중...")
    df = fetch_candles(args.market, args.candles)
    print(f"      기간: {df['time'].iloc[0]} ~ {df['time'].iloc[-1]} ({len(df)}봉)")

    print("[2/3] 백테스트 실행 중...")
    trades, metrics, equity = backtest(df, risk_pct=args.risk, rr_target=args.rr)

    print("[3/3] 결과")
    print("-" * 46)
    if metrics.get("trades", 0) == 0:
        print("신호가 없어 거래가 발생하지 않았습니다.")
        return
    print(f"총 거래        : {metrics['trades']}회 "
          f"(눌림목 {metrics['pullback_trades']} / 골든크로스 {metrics['goldencross_trades']})")
    print(f"승률           : {metrics['win_rate']:.1%}")
    print(f"평균 R         : {metrics['avg_r']:+.2f}R")
    print(f"Profit Factor  : {metrics['profit_factor']:.2f}")
    print(f"총 손익        : {metrics['total_pnl']:+,.0f}원 ({metrics['total_return']:+.2%})")
    print(f"최대 낙폭(MDD) : {metrics['max_drawdown']:.2%}")
    print("-" * 46)

    import os

    os.makedirs(args.out, exist_ok=True)
    trades.to_csv(f"{args.out}/trades.csv", index=False)
    equity.to_csv(f"{args.out}/equity.csv", index=False)
    print(f"거래 내역 -> {args.out}/trades.csv, 자산 곡선 -> {args.out}/equity.csv")


if __name__ == "__main__":
    main()
