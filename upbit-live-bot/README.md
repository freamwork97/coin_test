# Upbit Paper Trading Bot

업비트 페이퍼 트레이딩 봇 - 실거래 없이 전략을 검증합니다.

## ⚠️ 중요 안전장치

- **실거래 금지**: `live_trading_enabled=false` (기본값)
- **실거래 전환 시 명시적 승인 필요**
- **페이퍼 트레이딩만 가능**

## 설치 및 실행

### 1. 의존성 설치

```bash
pip install requests numpy
```

### 2. 설정

`config.json` 파일을 수정하여 전략과 안전장치를 설정하세요.

```json
{
  "paper_trading": {
    "fee_rate": 0.0005,
    "slippage": 0.0003,
    "initial_balance_krw": 10000000
  },
  "safety": {
    "max_daily_loss_rate": -0.05,
    "max_mdd_rate": -0.10,
    "max_single_coin_weight": 0.30,
    "consecutive_loss_stop": 5
  }
}
```

### 3. 실행

```bash
# 메인 루프 실행
python main.py

# 상태 확인
python main.py --status

# 리포트 생성
python main.py --report
```

## 아키텍처

```
upbit-paper-bot/
├── config.json          # 설정 파일
├── main.py              # 메인 루프
├── upbit_api.py         # 업비트 API (조회만)
├── paper_trader.py      # 페이퍼 트레이딩 엔진
├── safety.py            # 리스크 관리
├── performance.py       # 성과 측정
├── strategy.py          # 전략 모듈
├── reporter.py          # 텔레그램 리포트
├── trade_history.json   # 거래 내역
├── daily_pnl.json       # 일별 손익
└── bot.log              # 실행 로그
```

## 전략 개발

`strategy.py`에서 새로운 전략을 추가할 수 있습니다.

```python
class MyStrategy(BaseStrategy):
    def generate_signals(self, trader, markets):
        signals = []
        # ... 전략 로직 ...
        return signals
```

## 성과 지표

- **수익률**: 누적 수익률 (%)
- **MDD**: 최대 낙폭 (%)
- **승률**: 승리 거래 / 전체 거래
- **거래내역**: 모든 거래 기록
- **누적 수수료**: 총 수수료 지출
- **슬리피지 비용**: 총 슬리피지 비용

## 텔레그램 알림

- **매일 08:00**: 일일 성과 리포트
- **거래 실행 시**: 실시간 알림
- **안전장치 발동 시**: 경고 알림

## 라이선스

MIT
