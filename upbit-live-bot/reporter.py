"""
Reporter Module
Telegram reporting functionality
"""

import logging
import json
from typing import Dict, Optional
from datetime import datetime
import os

logger = logging.getLogger(__name__)

class TelegramReporter:
    def __init__(self, config: Dict):
        self.config = config
    
    def format_daily_report(self, performance_metrics) -> str:
        """Format daily report for Telegram"""
        report = performance_metrics.get_full_report()
        return performance_metrics.format_report_text(report)
    
    def format_trade_notification(self, trade) -> str:
        """Format trade notification"""
        emoji = "🟢" if trade.side == "buy" else "🔴"
        return f"""{emoji} *Paper Trade Executed*

Market: {trade.market}
Side: {trade.side.upper()}
Volume: {trade.volume:.8f}
Price: ₩{trade.price:,.0f}
Fee: ₩{trade.fee:,.2f}
Balance: ₩{trade.balance_after:,.0f}

{trade.timestamp}"""
    
    def format_safety_alert(self, safety_result: Dict) -> Optional[str]:
        """Format safety violation alert"""
        if safety_result["is_safe"]:
            return None
        
        violations = "\n".join([f"• {v}" for v in safety_result["violations"]])
        
        return f"""🚨 *Safety Alert*

Trading halted due to violations:
{violations}

Please review and take action."""
    
    def format_progress_report(self, status: Dict) -> str:
        """Format progress report during development"""
        return f"""📋 *Bot Progress Update*

Status: {status.get('phase', 'Unknown')}
Progress: {status.get('progress', 0)}%
Current Task: {status.get('current_task', 'N/A')}

Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
    
    def format_strategy_suggestion(self, analysis: Dict) -> str:
        """Format strategy improvement suggestion"""
        suggestions = "\n".join([f"• {s}" for s in analysis.get("suggestions", [])])
        
        return f"""💡 *Strategy Improvement Suggestion*

Based on recent performance:
{suggestions}

_This is a suggestion only. Auto-apply is disabled._

Analysis Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""

class ProgressLogger:
    """Log development progress to file"""
    
    def __init__(self, log_file: str = "progress.log"):
        self.log_file = os.path.expanduser(f"~/.openclaw/workspace/upbit-paper-bot/{log_file}")
    
    def log(self, message: str, level: str = "INFO"):
        """Log progress message"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}\n"
        
        with open(self.log_file, 'a') as f:
            f.write(log_entry)
        
        logger.info(message)
    
    def get_recent_logs(self, lines: int = 50) -> str:
        """Get recent log entries"""
        if not os.path.exists(self.log_file):
            return "No logs yet."
        
        with open(self.log_file, 'r') as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    
    def get_status(self) -> Dict:
        """Get current development status"""
        status_file = os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot/dev_status.json")
        
        if os.path.exists(status_file):
            with open(status_file, 'r') as f:
                return json.load(f)
        
        return {"phase": "not_started", "progress": 0, "current_task": "Initializing"}
    
    def update_status(self, phase: str, progress: int, current_task: str, details: Optional[Dict] = None):
        """Update development status"""
        status_file = os.path.expanduser("~/.openclaw/workspace/upbit-paper-bot/dev_status.json")
        
        status = {
            "phase": phase,
            "progress": progress,
            "current_task": current_task,
            "last_update": datetime.now().isoformat()
        }
        
        if details:
            status.update(details)
        
        with open(status_file, 'w') as f:
            json.dump(status, f, indent=2)
        
        self.log(f"Status update: {phase} - {progress}% - {current_task}")
