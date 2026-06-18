#!/bin/bash
# Start paper bot inside tmux session "paper-bot"
# Usage: ./start_bot.sh

set -e

SESSION="paper-bot"
BOT_DIR="$HOME/.openclaw/workspace/upbit-paper-bot"
PYTHON="$(which python3)"

cd "$BOT_DIR"

# Kill existing session if any
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Start new detached session
tmux new-session -d -s "$SESSION" -n bot "$PYTHON main.py"

# Create log window
tmux new-window -t "$SESSION" -n logs "tail -f logs/bot.log"

echo "✅ Paper bot started in tmux session '$SESSION'"
echo "   Attach:  tmux attach -t $SESSION"
echo "   Status:  $PYTHON main.py --status"
echo "   Report:  $PYTHON main.py --report"
