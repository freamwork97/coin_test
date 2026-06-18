"""
Upbit Paper Trading Bot — Main Event Loop
==========================================
- tmux-compatible continuous execution
- 1-min cycles: safety → signals → execute → equity snapshot
- Writes runtime/status.json, data/trades.jsonl, data/equity_curve.csv, logs/bot.log
- fee 0.05%, slippage 0.03% applied on every fill
- live_trading_enabled = false (NEVER calls real order API)
"""

import json
import os
import time
import logging
import signal
import sys
from datetime import datetime
from typing import Dict

BOT_DIR = os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot")
LOG_DIR = os.path.join(BOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging setup — file + stderr
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "bot.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Imports (after path setup so we can run from anywhere)
# ---------------------------------------------------------------------------
sys.path.insert(0, BOT_DIR)

from upbit_api import UpbitAPI
from paper_trader import PaperTrader
from safety import SafetyManager
from performance import PerformanceMetrics
from strategy import get_strategy


class PaperBot:
    def __init__(self, config_path: str = "config.json"):
        # Resolve path
        if not os.path.isabs(config_path):
            config_path = os.path.join(BOT_DIR, config_path)

        with open(config_path) as f:
            self.config = json.load(f)

        # Assert safety
        assert self.config["safety"]["live_trading_enabled"] is False, \
            "live_trading_enabled MUST be false"

        self.api = UpbitAPI()
        self.trader = PaperTrader(self.config, self.api)
        self.safety = SafetyManager(self.config)
        self.perf = PerformanceMetrics(self.trader)
        self.strategy = get_strategy(self.config["strategy"])

        self.markets = self.config["paper_trading"]["markets"]
        self.cycle_seconds = self.config["paper_trading"]["cycle_seconds"]
        self.running = False
        self.last_report_day = None

        logger.info("Bot initialized — strategy=%s  markets=%s  balance=₩%.0f",
                     self.config["strategy"]["name"], self.markets, self.trader.balance)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        self.running = True
        signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._handle_sigint)

        logger.info("=== Paper bot started ===")
        self._write_status()

        while self.running:
            cycle_start = time.time()
            try:
                # 1. Safety
                pv = self.trader.get_portfolio_value()
                safety = self.safety.check(self.trader, pv)

                if not safety["can_trade"]:
                    logger.warning("Trading halted: %s", safety["violations"])
                    self._write_status()
                    time.sleep(self.cycle_seconds)
                    continue

                # 2. Signals
                signals = self.strategy.generate_signals(self.api, self.markets)

                # 3. Execute — deduplicate per market (only first buy/sell per cycle)
                acted_buy = set()
                acted_sell = set()
                for sig in signals:
                    if sig.side == "buy" and sig.market not in acted_buy:
                        if sig.market not in self.trader.positions:
                            self._do_buy(sig)
                            acted_buy.add(sig.market)
                    elif sig.side == "sell" and sig.market not in acted_sell:
                        if sig.market in self.trader.positions:
                            self._do_sell(sig)
                            acted_sell.add(sig.market)

                # 4. Daily report (once per day around 08:00 KST)
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.hour == 8 and self.last_report_day != today:
                    self._daily_report()
                    self.last_report_day = today

                # 5. Status update
                self._write_status()

            except Exception as e:
                logger.error("Cycle error: %s", e, exc_info=True)

            # Sleep
            elapsed = time.time() - cycle_start
            remaining = max(0.5, self.cycle_seconds - elapsed)
            time.sleep(remaining)

        logger.info("=== Paper bot stopped ===")

    # ------------------------------------------------------------------
    # Trade execution helpers
    # ------------------------------------------------------------------
    def _do_buy(self, sig: Signal):
        pv = self.trader.get_portfolio_value()
        amount = self.trader.balance * self.trader.position_size_pct
        if amount < 5000:  # minimum meaningful trade
            return
        if not self.safety.check_single_coin_weight(self.trader, sig.market, amount, pv):
            logger.warning("Weight limit would be exceeded for %s — skip buy", sig.market)
            return
        self.trader.buy(sig.market, amount_krw=amount)

    def _do_sell(self, sig: Signal):
        self.trader.sell(sig.market)

    def _daily_report(self):
        try:
            report = self.perf.build_report()
            logger.info("Daily report: return=%.2f%%  MDD=%.2f%%  win_rate=%.1f%%",
                         report["returns"]["total_return_pct"],
                         report["mdd"]["mdd_pct"],
                         report["win_rate"]["win_rate_pct"])
        except Exception as e:
            logger.error("Daily report error: %s", e)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def _write_status(self):
        try:
            self.perf.build_report()
        except Exception as e:
            logger.error("Status write error: %s", e)

    def _handle_sigint(self, signum, frame):
        logger.info("Received signal %d — shutting down", signum)
        self.running = False


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser(description="Upbit Paper Trading Bot")
    p.add_argument("--config", default="config.json")
    p.add_argument("--report", action="store_true", help="Print report and exit")
    p.add_argument("--status", action="store_true", help="Print status and exit")
    args = p.parse_args()

    bot = PaperBot(args.config)

    if args.report:
        rep = bot.perf.build_report()
        print(PerformanceMetrics.format_text(rep))
        return

    if args.status:
        rep = bot.perf.build_report()
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return

    bot.run()


if __name__ == "__main__":
    main()
