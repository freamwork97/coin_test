"""
Performance Metrics
- Returns (absolute / %)
- MDD (Maximum Drawdown) from equity_curve.csv
- Win rate from trades.jsonl
- Fee & slippage summary
"""

import json
import os
import logging
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

BOT_DIR = os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot")
DATA_DIR = os.path.join(BOT_DIR, "data")
RUNTIME_DIR = os.path.join(BOT_DIR, "runtime")


class PerformanceMetrics:
    def __init__(self, trader):
        self.trader = trader

    # ------------------------------------------------------------------
    # Returns
    # ------------------------------------------------------------------
    def get_returns(self) -> dict:
        pv = self.trader.get_portfolio_value()
        iv = self.trader.initial_balance
        tr = pv - iv
        trr = tr / iv if iv > 0 else 0
        return {
            "initial_balance": iv,
            "current_balance": self.trader.balance,
            "portfolio_value": pv,
            "total_return": tr,
            "total_return_pct": trr * 100,
        }

    # ------------------------------------------------------------------
    # MDD from equity_curve.csv
    # ------------------------------------------------------------------
    def get_mdd(self) -> dict:
        values = self._load_equity_curve()
        if not values:
            return {"mdd_pct": 0.0, "mdd": 0.0, "peak": 0, "trough": 0}

        peak = values[0]
        trough = values[0]
        max_dd = 0.0
        dd_peak = values[0]
        dd_trough = values[0]

        for v in values:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                dd_peak = peak
                dd_trough = v

        return {"mdd": max_dd, "mdd_pct": max_dd * 100, "peak": dd_peak, "trough": dd_trough}

    def _load_equity_curve(self) -> List[float]:
        eq_file = os.path.join(DATA_DIR, "equity_curve.csv")
        if not os.path.exists(eq_file):
            return []
        vals = []
        with open(eq_file) as f:
            next(f, None)  # skip header
            for line in f:
                parts = line.strip().split(",")
                if len(parts) >= 2:
                    try:
                        vals.append(float(parts[1]))
                    except ValueError:
                        pass
        return vals

    # ------------------------------------------------------------------
    # Win rate from trades.jsonl
    # ------------------------------------------------------------------
    def get_win_rate(self) -> dict:
        trades_file = os.path.join(DATA_DIR, "trades.jsonl")
        if not os.path.exists(trades_file):
            return {"win_rate_pct": 0.0, "wins": 0, "losses": 0, "total_closed": 0}

        sells = []
        with open(trades_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                if t.get("side") == "sell":
                    sells.append(t)

        if not sells:
            return {"win_rate_pct": 0.0, "wins": 0, "losses": 0, "total_closed": 0}

        wins = sum(1 for t in sells if t.get("realized_pnl", 0) > 0)
        losses = sum(1 for t in sells if t.get("realized_pnl", 0) < 0)
        total = wins + losses
        wr = (wins / total * 100) if total > 0 else 0.0

        return {"win_rate_pct": wr, "wins": wins, "losses": losses, "total_closed": total}

    # ------------------------------------------------------------------
    # Fee summary
    # ------------------------------------------------------------------
    def get_fees(self) -> dict:
        return {
            "total_fees": self.trader.total_fees,
            "total_slippage": self.trader.total_slippage,
            "total_costs": self.trader.total_fees + self.trader.total_slippage,
            "fee_rate_pct": self.trader.fee_rate * 100,
            "slippage_rate_pct": self.trader.slippage * 100,
        }

    # ------------------------------------------------------------------
    # Trade summary
    # ------------------------------------------------------------------
    def get_trade_summary(self) -> dict:
        buys = [t for t in self.trader.trades if t["side"] == "buy"]
        sells = [t for t in self.trader.trades if t["side"] == "sell"]
        total_pnl = sum(t.get("realized_pnl", 0) for t in sells)
        return {
            "total_trades": len(self.trader.trades),
            "buys": len(buys),
            "sells": len(sells),
            "realized_pnl_total": total_pnl,
        }

    # ------------------------------------------------------------------
    # Positions snapshot
    # ------------------------------------------------------------------
    def get_positions(self) -> dict:
        unrealized = self.trader.get_unrealized_pnl()
        pv = self.trader.get_positions_value()
        return {
            "count": len(self.trader.positions),
            "markets": list(self.trader.positions.keys()),
            "total_value": pv,
            "unrealized_pnl": unrealized,
        }

    # ------------------------------------------------------------------
    # Full report → also writes runtime/status.json
    # ------------------------------------------------------------------
    def build_report(self) -> dict:
        ret = self.get_returns()
        mdd = self.get_mdd()
        wr = self.get_win_rate()
        fees = self.get_fees()
        ts = self.get_trade_summary()
        pos = self.get_positions()

        report = {
            "timestamp": datetime.now().isoformat(),
            "returns": ret,
            "mdd": mdd,
            "win_rate": wr,
            "fees": fees,
            "trades": ts,
            "positions": pos,
        }

        # Write runtime/status.json
        status_path = os.path.join(RUNTIME_DIR, "status.json")
        with open(status_path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        return report

    # ------------------------------------------------------------------
    # Text formatter
    # ------------------------------------------------------------------
    @staticmethod
    def format_text(report: dict) -> str:
        r = report["returns"]
        m = report["mdd"]
        w = report["win_rate"]
        f = report["fees"]
        t = report["trades"]
        p = report["positions"]

        return (
            f"📊 **Upbit Paper Bot — Report**\n\n"
            f"💰 Returns\n"
            f"• Portfolio: ₩{r['portfolio_value']:,.0f}\n"
            f"• Return: {r['total_return_pct']:+.2f}% (₩{r['total_return']:+,.0f})\n\n"
            f"📉 MDD: {m['mdd_pct']:.2f}%\n\n"
            f"🏆 Win Rate: {w['win_rate_pct']:.1f}% ({w['wins']}W/{w['losses']}L)\n\n"
            f"💸 Costs\n"
            f"• Fees: ₩{f['total_fees']:,.2f} ({f['fee_rate_pct']:.2f}%)\n"
            f"• Slippage: ₩{f['total_slippage']:,.2f} ({f['slippage_rate_pct']:.2f}%)\n"
            f"• Total: ₩{f['total_costs']:,.2f}\n\n"
            f"📈 Trades: {t['total_trades']} ({t['buys']}B/{t['sells']}S) | "
            f"Realized PnL: ₩{t['realized_pnl_total']:+,.0f}\n\n"
            f"📍 Positions: {p['count']} active (₩{p['total_value']:,.0f})\n\n"
            f"⏰ {report['timestamp'][:19]}"
        )
