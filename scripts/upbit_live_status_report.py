#!/usr/bin/env python3
"""Read-only Upbit live bot status report."""
from __future__ import annotations

import csv
import json
import math
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

BOT_DIR = Path('/home/windra/.openclaw/workspace')
STATUS_FILE = BOT_DIR / 'runtime/status.json'
TRADES_FILE = BOT_DIR / 'data/live_trades.jsonl'
EQUITY_FILE = BOT_DIR / 'data/live_equity_curve.csv'
LOG_FILE = BOT_DIR / 'logs/bot.log'
SERVICE = 'upbit-live-bot.service'
KST = timezone(timedelta(hours=9))


def won(v: float | int | str | None) -> str:
    try:
        return f"₩{float(v):,.0f}"
    except Exception:
        return 'n/a'


def pct(v: float | int | str | None) -> str:
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return 'n/a'


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=10)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return 999, str(exc)


def read_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    return json.loads(STATUS_FILE.read_text(encoding='utf-8'))


def read_trades() -> list[dict]:
    trades = []
    if not TRADES_FILE.exists():
        return trades
    for line in TRADES_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            trades.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return trades


def read_equity() -> list[tuple[str, float]]:
    if not EQUITY_FILE.exists():
        return []
    rows = []
    with EQUITY_FILE.open(newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            try:
                rows.append((row.get('timestamp') or '', float(row.get('equity') or 0)))
            except ValueError:
                pass
    return rows


def mdd(rows: list[tuple[str, float]]) -> dict:
    peak = -math.inf
    peak_ts = ''
    max_dd = 0.0
    trough = 0.0
    trough_ts = ''
    for ts, eq in rows:
        if eq > peak:
            peak = eq
            peak_ts = ts
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
                trough = eq
                trough_ts = ts
    return {'mdd_pct': max_dd * 100, 'peak': peak if peak != -math.inf else 0, 'peak_ts': peak_ts, 'trough': trough, 'trough_ts': trough_ts}


def recent_log_counts(lines: int = 300) -> dict:
    if not LOG_FILE.exists():
        return {'warnings': 0, 'errors': 0, 'rate_limits': 0, 'safety_halts': 0, 'last_warnings': []}
    all_lines = LOG_FILE.read_text(encoding='utf-8', errors='ignore').splitlines()[-lines:]
    warnings = [l for l in all_lines if '[WARNING]' in l]
    errors = [l for l in all_lines if '[ERROR]' in l]
    rate_limits = [l for l in all_lines if 'Rate limit' in l or '429' in l]
    safety_halts = [l for l in all_lines if 'Trading halted' in l or 'Safety violations' in l]
    return {
        'warnings': len(warnings),
        'errors': len(errors),
        'rate_limits': len(rate_limits),
        'safety_halts': len(safety_halts),
        'last_warnings': warnings[-5:],
        'last_errors': errors[-5:],
    }


def trade_summary(trades: list[dict]) -> dict:
    buys = [t for t in trades if t.get('side') == 'buy']
    sells = [t for t in trades if t.get('side') == 'sell']
    fees = 0.0
    zero_fee_or_volume = 0
    for t in trades:
        try:
            fees += float(t.get('fee') or 0)
        except Exception:
            pass
        try:
            vol = float(t.get('volume_executed') or t.get('volume') or 0)
            fee = float(t.get('fee') or 0)
            if vol == 0 or fee == 0:
                zero_fee_or_volume += 1
        except Exception:
            zero_fee_or_volume += 1
    return {
        'total': len(trades),
        'buys': len(buys),
        'sells': len(sells),
        'fees': fees,
        'zero_fee_or_volume': zero_fee_or_volume,
        'last': trades[-5:],
    }


def main() -> int:
    status = read_status()
    trades = read_trades()
    equity = read_equity()
    m = mdd(equity)
    logs = recent_log_counts()
    ts = trade_summary(trades)
    svc_code, svc_out = run(['systemctl', '--user', 'is-active', SERVICE])
    pgrep_code, pgrep_out = run(['pgrep', '-af', 'main_live.py --config config_live.json'])

    initial = equity[0][1] if equity else None
    latest = equity[-1][1] if equity else status.get('portfolio_value')
    ret = ((latest - initial) / initial * 100) if initial and latest else status.get('total_return_pct')

    positions = status.get('positions') or {}
    health = 'ok'
    notes = []
    if svc_out.strip() != 'active':
        health = 'error'
        notes.append(f'service={svc_out or svc_code}')
    if logs['errors']:
        health = 'error'
        notes.append(f"recent_errors={logs['errors']}")
    if logs['rate_limits']:
        if health == 'ok':
            health = 'watch'
        notes.append(f"rate_limits={logs['rate_limits']}")
    if logs['safety_halts']:
        if health == 'ok':
            health = 'watch'
        notes.append(f"safety_halts={logs['safety_halts']}")
    if ts['zero_fee_or_volume']:
        if health == 'ok':
            health = 'watch'
        notes.append('trade_records_have_zero_fee_or_volume')

    now = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')
    print(f"# Upbit Live Status ({now})")
    print()
    print(f"- Health: {health}" + (f" ({', '.join(notes)})" if notes else ''))
    print(f"- Service: {svc_out.strip() or 'unknown'}")
    print(f"- Process: {'present' if pgrep_code == 0 and pgrep_out else 'missing'}")
    print(f"- Portfolio: {won(latest)} / KRW: {won(status.get('krw_balance'))} / Positions value: {won(status.get('positions_value'))}")
    print(f"- Return: {pct(ret)} / MDD: {-m['mdd_pct']:.2f}%")
    print(f"- Positions: {len(positions)} active")
    for market, pos in positions.items():
        print(f"  - {market}: volume={pos.get('volume')} unrealized={won(pos.get('unrealized_pnl'))}")
    print(f"- Trades: {ts['total']} ({ts['buys']} buy / {ts['sells']} sell) / recorded fees: {won(ts['fees'])}")
    if ts['zero_fee_or_volume']:
        print(f"- Data quality: {ts['zero_fee_or_volume']} trade record(s) have zero fee or zero executed volume; win rate/fee report is limited until order detail recording is improved.")
    print(f"- Recent log scan: warnings={logs['warnings']}, errors={logs['errors']}, rate_limits={logs['rate_limits']}, safety_halts={logs['safety_halts']}")
    if logs['last_errors']:
        print("- Last errors:")
        for line in logs['last_errors']:
            print(f"  - {line[-240:]}")
    elif logs['last_warnings']:
        print("- Last warnings:")
        for line in logs['last_warnings'][-3:]:
            print(f"  - {line[-240:]}")
    return 0 if health != 'error' else 2


if __name__ == '__main__':
    raise SystemExit(main())
