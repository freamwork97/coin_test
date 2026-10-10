#!/usr/bin/env python3
"""Refresh daily backtest cache through today (public API, no auth)."""
import glob, json, os, time, urllib.request

CACHE = "/home/windra/.openclaw/workspace/upbit-live-bot/data/backtest"
TOTAL = 800


def fetch(market, total=TOTAL):
    rows, to = [], None
    while len(rows) < total:
        url = (f"https://api.upbit.com/v1/candles/days?market={market}"
               f"&count={min(200, total - len(rows))}" + (f"&to={to}" if to else ""))
        with urllib.request.urlopen(url, timeout=15) as r:
            batch = json.load(r)
        if not batch:
            break
        rows.extend(batch)
        to = batch[-1]["candle_date_time_utc"]
        time.sleep(0.12)
    seen = {c["candle_date_time_utc"]: c for c in rows}
    return sorted(seen.values(), key=lambda c: c["candle_date_time_utc"])


markets = sorted(os.path.basename(f)[: -len("_day.json")]
                 for f in glob.glob(os.path.join(CACHE, "*_day.json")))
for m in markets:
    try:
        data = fetch(m)
        json.dump(data, open(os.path.join(CACHE, f"{m}_day.json"), "w"))
        print(f"  {m}: {len(data)}  {data[0]['candle_date_time_kst'][:10]} → "
              f"{data[-1]['candle_date_time_kst'][:10]}", flush=True)
    except Exception as e:
        print(f"  {m}: ERROR {e}", flush=True)
