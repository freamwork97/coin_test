#!/usr/bin/env bash
# Live bot watchdog
set -euo pipefail
LOG_FILE="$HOME/.openclaw/workspace/upbit-live-bot/logs/watchdog.log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }
SESSION="live-bot"
if tmux has-session -t "$SESSION" 2>/dev/null; then
    pid=$(tmux list-panes -t "$SESSION" -F "#{pane_pid}" 2>/dev/null | head -1)
    if [ -n "$pid" ] && ps -p "$pid" -o comm= 2>/dev/null | grep -q python3; then
        exit 0
    fi
    log "Pane dead, restarting..."
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    sleep 2
else
    log "Session missing, starting..."
fi
cd "$HOME/.openclaw/workspace/upbit-live-bot"
tmux new-session -d -s "$SESSION" -n bot "/usr/bin/python3 main_live.py --config config_live.json"
log "Started."
