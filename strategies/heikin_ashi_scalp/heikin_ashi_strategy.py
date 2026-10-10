"""
하이킨아시 5분봉 단타 전략 코어.

유튜브 '하이킨아시 5분봉 단타매매법'(차트 승부사 고니) 영상 분석 기반:
- 차트 종류: 하이킨아시 (트레이딩뷰 차트 타입)
- EMA 20: 눌림목 판단 기준선
- EMA 10 + EMA 20: 더블 이동평균선 결합 (골든크로스 추세 추종)

중요: 트레이딩뷰에서 차트 종류가 하이킨아시일 때 지표는 하이킨아시 값 기준으로
계산된다. 영상 화면과 동일한 조건을 만들기 위해 EMA는 ha_close 기준으로 계산하고,
진입/손절/익절 가격은 실제 호가(close/high/low) 기준으로 처리한다.

손절·익절 기준은 영상에 명시되지 않아 실전 일반 기준으로 보완한 부분이다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FEE_RATE = 0.0005  # 업비트 0.05% / 편도


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """OHLC -> 하이킨아시 변환. 입력 컬럼: open, high, low, close"""
    df = df.copy()
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]

    ha_close = (o + h + l + c) / 4.0
    ha_open = pd.Series(np.nan, index=df.index, dtype=float)
    ha_open.iloc[0] = (o.iloc[0] + c.iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0

    ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1)

    df["ha_open"] = ha_open
    df["ha_high"] = ha_high
    df["ha_low"] = ha_low
    df["ha_close"] = ha_close
    return df


def add_indicators(
    df: pd.DataFrame,
    ema_fast: int = 10,
    ema_slow: int = 20,
    touch_tol: float = 0.003,
    push_pct: float = 0.005,
    push_lookback: int = 10,
    swing_n: int = 10,
) -> pd.DataFrame:
    """지표 + 매수 시그널 생성. 시그널은 완성된 봉 기준으로 계산된다."""
    df = heikin_ashi(df)

    df["ema_fast"] = df["ha_close"].ewm(span=ema_fast, adjust=False).mean()
    df["ema_slow"] = df["ha_close"].ewm(span=ema_slow, adjust=False).mean()
    df["ha_bull"] = df["ha_close"] > df["ha_open"]
    df["trend_up"] = df["ema_fast"] > df["ema_slow"]

    # A. 20EMA 눌림목: 최근 push_lookback봉 안에 선 위로 push_pct 이상 치솟았다가
    #    지금 20EMA에 닿은(눌린) 양봉. 5분봉은 선에 바짝 붙어 움직이므로
    #    '위에서 닿음'이 아니라 '치솟은 뒤 눌림'으로 정의한다.
    touched = df["ha_low"] <= df["ema_slow"] * (1 + touch_tol)
    dist_high = (df["ha_high"] - df["ema_slow"]) / df["ema_slow"]
    recent_push = dist_high.shift(1).rolling(push_lookback).max() >= push_pct
    df["sig_pullback"] = (df["trend_up"] & df["ha_bull"] & touched & recent_push).fillna(False)

    # B. 골든크로스: 10EMA가 20EMA를 상향 돌파 + 하이킨아시 양봉
    crossed = (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)) & (
        df["ema_fast"] > df["ema_slow"]
    )
    df["sig_goldencross"] = (crossed & df["ha_bull"]).fillna(False)

    df["signal"] = df["sig_pullback"] | df["sig_goldencross"]

    # 손절 기준: 직전 N봉 최저가 (스윙 로우)
    df["swing_low"] = df["low"].shift(1).rolling(swing_n).min()
    return df


def backtest(
    df: pd.DataFrame,
    risk_pct: float = 0.01,
    rr_target: float = 1.5,
    initial_equity: float = 10_000_000,
) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """
    단순 백테스트. 시그널 발생 다음 봉 시가에 진입한다.
    - 손절: 진입 시점 스윙 로우
    - 익절: 손절폭의 rr_target 배
    - 추세 이탈 청산: 종가가 2봉 연속 ema_slow 아래 / 데드크로스
    반환: (trades, metrics, equity_curve)
    """
    df = add_indicators(df).reset_index(drop=True)
    n = len(df)
    if n < 60:
        raise ValueError("백테스트에는 최소 60개 이상의 봉이 필요합니다.")

    equity = initial_equity
    trades: list[dict] = []
    equity_curve = [{"t": df.index[0], "equity": equity}]
    pos = None  # dict(entry, stop, target, units, entry_i, kind)
    break_count = 0

    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    ema_slow = df["ema_slow"].to_numpy()
    ema_fast = df["ema_fast"].to_numpy()
    signal = df["signal"].to_numpy()
    sig_pb = df["sig_pullback"].to_numpy()
    swing_low = df["swing_low"].to_numpy()

    def close_position(exit_price: float, exit_i: int, reason: str):
        nonlocal equity
        units = pos["units"]
        cost_in = units * pos["entry"] * (1 + FEE_RATE)
        proceeds = units * exit_price * (1 - FEE_RATE)
        pnl = proceeds - cost_in
        risk_unit = pos["entry"] - pos["stop"]
        r_mult = (exit_price - pos["entry"] - pos["entry"] * FEE_RATE - exit_price * FEE_RATE) / risk_unit
        equity += pnl
        trades.append(
            {
                "entry_i": pos["entry_i"],
                "exit_i": exit_i,
                "kind": pos["kind"],
                "entry": pos["entry"],
                "stop": pos["stop"],
                "target": pos["target"],
                "exit": exit_price,
                "reason": reason,
                "units": units,
                "pnl": pnl,
                "r_multiple": r_mult,
                "equity": equity,
            }
        )
        equity_curve.append({"t": exit_i, "equity": equity})

    for i in range(50, n - 1):
        if pos is None:
            if bool(signal[i]):
                entry = float(opens[i + 1])
                stop = float(swing_low[i])
                risk = entry - stop
                if risk <= 0 or np.isnan(risk):
                    continue
                target = entry + rr_target * risk
                risk_amount = equity * risk_pct
                units = risk_amount / risk
                pos = {
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "units": units,
                    "entry_i": i + 1,
                    "kind": "pullback" if bool(sig_pb[i]) else "goldencross",
                }
                break_count = 0
            continue

        # 보유 중: 다음 봉(i+1 아님, 현재 i봉)으로 청산 조건 체크
        # 실제 운용을 단순화해 시그널 봉 다음 봉부터 매 봉 체크
        bar_o, bar_h, bar_l, bar_c = opens[i], highs[i], lows[i], closes[i]

        # 1) 손절/익절 (같은 봉에 둘 다 닿으면 보수적으로 손절 우선)
        if bar_l <= pos["stop"]:
            close_position(pos["stop"], i, "stop")
            pos = None
            continue
        if bar_h >= pos["target"]:
            close_position(pos["target"], i, "target")
            pos = None
            continue

        # 2) 추세 이탈: 종가 2봉 연속 ema_slow 하회
        if bar_c < ema_slow[i]:
            break_count += 1
        else:
            break_count = 0
        if break_count >= 2 or ema_fast[i] < ema_slow[i]:
            close_position(bar_c, i, "trend_break")
            pos = None
            continue

    # 마지막에 포지션 남아있으면 종가 청산
    if pos is not None:
        close_position(float(closes[-1]), n - 1, "eod")
        pos = None

    trades_df = pd.DataFrame(trades)
    metrics = _metrics(trades_df, initial_equity, equity)
    eq_df = pd.DataFrame(equity_curve)
    return trades_df, metrics, eq_df


def _metrics(trades_df: pd.DataFrame, initial: float, final: float) -> dict:
    n = len(trades_df)
    if n == 0:
        return {"trades": 0}
    wins = trades_df[trades_df["pnl"] > 0]
    gross_win = wins["pnl"].sum()
    gross_loss = -trades_df[trades_df["pnl"] <= 0]["pnl"].sum()
    eq = trades_df["equity"].to_numpy()
    peak = np.maximum.accumulate(np.concatenate([[initial], eq]))
    dd = (np.concatenate([[initial], eq]) - peak) / peak
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "avg_r": trades_df["r_multiple"].mean(),
        "expectancy_r": trades_df["r_multiple"].mean(),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "total_pnl": trades_df["pnl"].sum(),
        "total_return": (final - initial) / initial,
        "max_drawdown": dd.min(),
        "pullback_trades": int((trades_df["kind"] == "pullback").sum()),
        "goldencross_trades": int((trades_df["kind"] == "goldencross").sum()),
    }
