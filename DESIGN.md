# Upbit Paper Trading Bot - Design Document

## Architecture

```
upbit-paper-bot/
├── config.json              # Trading parameters, fees, safety limits
├── upbit_api.py             # Upbit API wrapper (read-only + paper trading)
├── paper_trader.py          # Paper trading engine
├── safety.py                # Risk management & safety checks
├── performance.py           # Performance metrics (returns, MDD, win rate, etc.)
├── strategy.py              # Trading strategy templates
├── reporter.py              # Telegram report generation
├── main.py                  # Main event loop
├── trade_history.json       # Record of all paper trades
├── daily_pnl.json           # Daily profit/loss records
├── progress.log             # Development progress log
└── README.md                # Setup and usage guide
```

## Module Responsibilities

### config.json
- fee_rate: 0.0005 (0.05%)
- slippage: 0.0003 (0.03%)
- live_trading_enabled: false (NEVER change without explicit approval)
- max_daily_loss_rate: -0.05 (5%)
- max_mdd_rate: -0.10 (10%)
- max_single_coin_weight: 0.30 (30%)
- consecutive_loss_stop: 5
- initial_balance: 10,000,000 KRW

### upbit_api.py
- Public API: market list, candle data, ticker, orderbook
- Paper order simulation (NO real orders)
- Rate limiting

### paper_trader.py
- Simulate buy/sell with fee + slippage
- Calculate actual fill price
- Track virtual balance and positions

### safety.py
- Check daily loss limit
- Check MDD limit
- Check single coin weight limit
- Check consecutive loss count
- Emergency stop mechanism

### performance.py
- Calculate returns, MDD, win rate
- Track cumulative fees, slippage costs
- Generate performance summary

### strategy.py
- Base strategy class
- Example: Moving Average Crossover
- Signal generation only (no execution)

### reporter.py
- Daily report at 08:00 KST
- Progress updates every 10 minutes during development
- Telegram integration

## Safety First

1. **NEVER** call real order API
2. **NEVER** enable live_trading without explicit approval
3. All paper trades log to trade_history.json
4. All config changes logged

## Implementation Order

1. Create directory structure and config
2. Implement upbit_api.py (read-only)
3. Implement paper_trader.py
4. Implement safety.py
5. Implement performance.py
6. Implement strategy.py
7. Implement reporter.py
8. Implement main.py with event loop
9. Test and validate
10. Setup cron jobs for reporting
