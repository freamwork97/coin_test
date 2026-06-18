#!/usr/bin/env python3
"""
Cron wrapper for daily report at 08:00 KST
"""

import sys
import os

sys.path.insert(0, os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot"))

from main import UpbitPaperBot

if __name__ == "__main__":
    bot = UpbitPaperBot("config.json")
    
    # Generate report
    report = bot.performance.get_full_report()
    text = bot.performance.format_report_text(report)
    
    # Save to file for Telegram delivery
    report_file = os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot/daily_report.txt")
    with open(report_file, 'w') as f:
        f.write(text)
    
    print(text)
