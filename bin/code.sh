#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="claude-pico"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already running elsewhere; attaching read-only."
  echo "(Ctrl-b d to detach without stopping it.)"
  exec tmux attach -t "$SESSION" -r
fi

tmux new-session -d -s "$SESSION" "$ROOT_DIR/bin/code_loop.sh"
exec tmux attach -t "$SESSION"
