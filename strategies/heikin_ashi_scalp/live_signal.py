"""
실시간 시그널 체크. 최신 봉 기준으로 현재 상태를 출력한다.

사용법:
    python live_signal.py --market KRW-BTC

매매 판단은 본인이 직접. 이 스크립트는 보조 판단용이다.
"""

from __future__ import annotations

import argparse

from backtest import fetch_candles
from heikin_ashi_strategy import add_indicators


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="KRW-BTC")
    args = ap.parse_args()

    df = add_indicators(fetch_candles(args.market, 120))
    last = df.iloc[-1]
    prev = df.iloc[-2]

    trend = "상승 정배열 (10EMA > 20EMA)" if last["trend_up"] else "하락/혼조 (10EMA <= 20EMA)"
    ha = "양봉(녹색)" if last["ha_bull"] else "음봉(빨강)"
    dist = (last["ha_low"] - last["ema_slow"]) / last["ema_slow"] * 100

    print(f"[{args.market}] {last['time']} (5분봉)")
    print(f"현재가      : {last['close']:,.0f}")
    print(f"추세        : {trend}")
    print(f"하이킨아시  : {ha}")
    print(f"20EMA 거리  : {dist:+.2f}% (양수=선 위, 음수=선 아래)")
    print()
    print("진입 체크리스트:")
    print(f"  [ {'v' if last['trend_up'] else ' '} ] 10EMA > 20EMA")
    print(f"  [ {'v' if last['ha_bull'] else ' '} ] 하이킨아시 양봉 연속")
    print(f"  [ {'v' if last['sig_pullback'] else ' '} ] 눌림목 신호 (선 터치 후 양봉 전환)")
    print(f"  [ {'v' if last['sig_goldencross'] else ' '} ] 골든크로스 신호")
    print()
    if last["signal"]:
        kind = "눌림목 매수" if last["sig_pullback"] else "골든크로스"
        print(f">> 매수 신호 발생 ({kind}). 손절가 후보(직전 10봉 저가): {last['swing_low']:,.0f}")
        print("   손절가·수량은 직접 계산 후 진입할 것.")
    else:
        print(">> 신호 없음. 관망.")


if __name__ == "__main__":
    main()
