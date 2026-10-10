#!/usr/bin/env python3
"""Read-only dry run of the V9 strategy against live Upbit data. NO ORDERS."""
import json
import sys

sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot")

from live_api import LiveUpbitAPI
from strategy_live import get_strategy

cfg = json.load(open("/home/windra/.openclaw/workspace/upbit-live-bot/config_live.json"))
api = LiveUpbitAPI()
strat = get_strategy(cfg["strategy"])
markets = cfg["live_trading"]["markets"]

print(f"strategy = {cfg['strategy']['name']}  classes={type(strat).__name__}")
sigs = strat.generate_signals(api, markets)

buys = [s for s in sigs if s.side == "buy"]
sells = [s for s in sigs if s.side == "sell"]
print(f"\nBUY ({len(buys)}):")
for s in buys:
    print(f"  {s.market:10s} weight={s.params.get('weight'):.2f} role={s.params.get('role')}  {s.reason}")
print(f"\nSELL ({len(sells)}):")
for s in sells[:12]:
    print(f"  {s.market:10s} {s.reason}")

total = sum(float(s.params.get("weight", 0)) for s in buys)
print(f"\ntarget gross exposure = {total:.0%}")
