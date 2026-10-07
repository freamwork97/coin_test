#!/usr/bin/env python3
"""
Historical data fetcher for backtesting (Upbit public API, read-only).
Caches OHLCV to data/backtest/<market>_<unit>.json
"""
import sys, os, json, time, argparse
sys.path.insert(0, "/home/windra/.openclaw/workspace/upbit-live-bot")
from live_api import LiveUpbitAPI

CACHE = "/home/windra/.openclaw/workspace/upbit-live-bot/data/backtest"
os.makedirs(CACHE, exist_ok=True)

# Liquid KRW universe (top by 24h volume — verified live)
UNIVERSE = [
    "KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-DOGE", "KRW-ADA",
    "KRW-AVAX", "KRW-LINK", "KRW-DOT", "KRW-MATIC", "KRW-ATOM", "KRW-XLM",
    "KRW-NEAR", "KRW-SUI", "KRW-APT", "KRW-ARB", "KRW-OP", "KRW-INJ",
    "KRW-SEI", "KRW-TIA", "KRW-STX", "KRW-ETC", "KRW-BCH", "KRW-UNI",
]


def fetch_paged(api, market, unit, total=800):
    """Fetch `total` candles using the bot's own paged fetcher (handles Upbit's
    200-per-request cap and `to` pagination). Returns ascending by time."""
    if unit == "day":
        out = api.get_day_candles(market, total)
    else:
        out = api.get_candles(market, unit, total)
    seen = {}
    for c in out:
        seen[c["candle_date_time_utc"]] = c
    return sorted(seen.values(), key=lambda c: c["candle_date_time_utc"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--unit", default="day", help="day | 240 | 60")
    p.add_argument("--total", type=int, default=800)
    p.add_argument("--markets", default=",".join(UNIVERSE))
    args = p.parse_args()

    api = LiveUpbitAPI()
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    for m in markets:
        path = os.path.join(CACHE, f"{m}_{args.unit}.json")
        if os.path.exists(path):
            try:
                d = json.load(open(path))
                if len(d) >= args.total * 0.9:
                    print(f"  {m}: cached ({len(d)})")
                    continue
            except Exception:
                pass
        print(f"  {m}: fetching {args.total} {args.unit} candles...")
        data = fetch_paged(api, m, args.unit, args.total)
        json.dump(data, open(path, "w"))
        span = f"{data[0]['candle_date_time_kst'][:10]} → {data[-1]['candle_date_time_kst'][:10]}" if data else "EMPTY"
        print(f"     got {len(data)}  [{span}]")


if __name__ == "__main__":
    main()
