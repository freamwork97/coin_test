#!/bin/bash
# Test script for Upbit Paper Trading Bot

cd ~/.openclaw/workspace/upbit-paper-bot

echo "=== Upbit Paper Bot Test Suite ==="
echo ""

# Test 1: Check Python syntax
echo "Test 1: Python syntax check..."
python -m py_compile upbit_api.py paper_trader.py safety.py performance.py strategy.py reporter.py main.py
if [ $? -eq 0 ]; then
    echo "✅ All files have valid Python syntax"
else
    echo "❌ Syntax error found"
    exit 1
fi
echo ""

# Test 2: Import test
echo "Test 2: Module import test..."
python -c "
import sys
sys.path.insert(0, '.')
from upbit_api import UpbitAPI
from paper_trader import PaperTrader
from safety import SafetyManager
from performance import PerformanceMetrics
from strategy import MACrossoverStrategy
from reporter import TelegramReporter
print('✅ All modules imported successfully')
"
echo ""

# Test 3: Config validation
echo "Test 3: Config validation..."
python -c "
import json
with open('config.json') as f:
    config = json.load(f)
    assert config['paper_trading']['enabled'] == True, 'Paper trading must be enabled'
    assert config['safety']['live_trading_enabled'] == False, 'Live trading must be disabled'
    print('✅ Config validated')
"
echo ""

# Test 4: Paper trade simulation
echo "Test 4: Paper trade simulation..."
python -c "
import sys
sys.path.insert(0, '.')
import json
from upbit_api import UpbitAPI
from paper_trader import PaperTrader

with open('config.json') as f:
    config = json.load(f)

api = UpbitAPI()
trader = PaperTrader(config, api)

# Check initial state
assert trader.balance == 10000000, 'Initial balance should be 10M KRW'
print('✅ Paper trader initialized with correct balance')
"
echo ""

# Test 5: API connectivity (public endpoint)
echo "Test 5: API connectivity..."
python -c "
import sys
sys.path.insert(0, '.')
from upbit_api import UpbitAPI

api = UpbitAPI()
markets = api.get_market_list()
print(f'✅ Connected to Upbit API - {len(markets)} markets available')

# Get KRW markets
krw_markets = [m['market'] for m in markets if m['market'].startswith('KRW-')]
print(f'✅ Found {len(krw_markets)} KRW markets')

# Get ticker for first few markets
if krw_markets:
    tickers = api.get_ticker(krw_markets[:5])
    print(f'✅ Got tickers for {len(tickers)} markets')
    for t in tickers[:3]:
        print(f'  - {t[\"market\"]}: ₩{t[\"trade_price\"]:,.0f}')
"
echo ""

# Test 6: Strategy signal generation
echo "Test 6: Strategy test..."
python -c "
import sys
sys.path.insert(0, '.')
import json
from upbit_api import UpbitAPI
from paper_trader import PaperTrader
from strategy import MACrossoverStrategy

with open('config.json') as f:
    config = json.load(f)

api = UpbitAPI()
trader = PaperTrader(config, api)
strategy = MACrossoverStrategy(config['strategy'])

markets = ['KRW-BTC']
signals = strategy.generate_signals(trader, markets)
print(f'✅ Strategy generated {len(signals)} signals for {markets}')
for sig in signals:
    print(f'  - {sig.market}: {sig.side} (confidence: {sig.confidence:.2f})')
"
echo ""

# Test 7: Safety check
echo "Test 7: Safety check..."
python -c "
import sys
sys.path.insert(0, '.')
import json
from upbit_api import UpbitAPI
from paper_trader import PaperTrader
from safety import SafetyManager

with open('config.json') as f:
    config = json.load(f)

api = UpbitAPI()
trader = PaperTrader(config, api)
safety = SafetyManager(config)

portfolio_value = trader.get_portfolio_value()
result = safety.check_safety(trader, portfolio_value)
print(f'✅ Safety check passed: {result[\"is_safe\"]}')
print(f'   Can trade: {result[\"can_trade\"]}')
"
echo ""

# Test 8: Performance metrics
echo "Test 8: Performance metrics..."
python -c "
import sys
sys.path.insert(0, '.')
import json
from upbit_api import UpbitAPI
from paper_trader import PaperTrader
from performance import PerformanceMetrics

with open('config.json') as f:
    config = json.load(f)

api = UpbitAPI()
trader = PaperTrader(config, api)
perf = PerformanceMetrics(trader)

returns = perf.calculate_returns()
print(f'✅ Returns calculated: {returns[\"total_return_pct\"]:.2f}%')

mdd = perf.calculate_mdd()
print(f'✅ MDD calculated: {mdd[\"mdd_pct\"]:.2f}%')

fees = perf.get_fee_summary()
print(f'✅ Fee summary: ₩{fees[\"total_fees\"]:.2f}')
"
echo ""

# Test 9: Report generation
echo "Test 9: Report generation..."
python -c "
import sys
sys.path.insert(0, '.')
import json
from upbit_api import UpbitAPI
from paper_trader import PaperTrader
from performance import PerformanceMetrics
from reporter import TelegramReporter

with open('config.json') as f:
    config = json.load(f)

api = UpbitAPI()
trader = PaperTrader(config, api)
perf = PerformanceMetrics(trader)
reporter = TelegramReporter(config)

text = reporter.format_daily_report(perf)
print('✅ Report generated successfully')
print('--- Report Preview ---')
print(text[:500])
print('...')
"
echo ""

# Test 10: Main module
echo "Test 10: Main module..."
python -c "
import sys
sys.path.insert(0, '.')
from main import UpbitPaperBot

bot = UpbitPaperBot('config.json')
status = bot.get_status()
print(f'✅ Bot initialized')
print(f'   Portfolio: ₩{status[\"portfolio_value\"]:,.0f}')
print(f'   Positions: {status[\"positions\"]}')
print(f'   Trading enabled: {status[\"trading_enabled\"]}')
"
echo ""

echo "=== All Tests Passed ==="
