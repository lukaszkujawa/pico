#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="$ROOT_DIR/tasks"
TODO_DIR="$TASKS_DIR/todo"
DONE_DIR="$TASKS_DIR/done"
PROMPT_FILE="$TASKS_DIR/PROMPT.md"
STOP_FILE="$ROOT_DIR/.stop_code"
MAX_STEPS="${MAX_STEPS:-50}"
SESSION="claude-pico"
FORMAT_FILTER="$ROOT_DIR/bin/format_stream.jq"

next_task() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | sort | head -n 1
}

todo_count() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' '
}

run_claude_in_tmux() {
  local log_file exit_file
  log_file="$(mktemp)"
  exit_file="$(mktemp)"

  tmux kill-session -t "$SESSION" 2>/dev/null || true

  tmux new-session -d -s "$SESSION" -x 220 -y 50 bash -c \
    "claude -p \"\$(cat '$PROMPT_FILE')\" --dangerously-skip-permissions --disallowedTools AskUserQuestion --verbose --output-format stream-json | jq -r -f '$FORMAT_FILTER'; echo \${PIPESTATUS[0]} > '$exit_file'; tmux wait-for -S ${SESSION}_done"

  tmux pipe-pane -t "$SESSION" -o "cat >> '$log_file'"

  tail -n +1 -f "$log_file" &
  local tail_pid=$!

  tmux wait-for "${SESSION}_done"

  sleep 0.2
  kill "$tail_pid" 2>/dev/null || true
  wait "$tail_pid" 2>/dev/null || true

  local exit_code
  exit_code="$(cat "$exit_file" 2>/dev/null || echo 1)"

  rm -f "$log_file" "$exit_file"

  tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"

  return "$exit_code"
}

cd "$ROOT_DIR"

for ((step = 1; step <= MAX_STEPS; step++)); do
  before_count=$(todo_count)

  if (( before_count == 0 )); then
    echo
    echo "All tasks complete."
    exit 0
  fi

  before_task=$(next_task)

  echo
  echo "============================================================"
  echo "STEP $step/$MAX_STEPS | $before_count task(s) remaining | next: $(basename "$before_task")"
  echo "Attach live with: tmux attach -t $SESSION"
  echo "============================================================"
  echo

  run_claude_in_tmux
  claude_exit=$?

  if [[ -f "$STOP_FILE" ]]; then
    rm -f "$STOP_FILE"
    echo
    echo "Stop requested via .stop_code. Stopping after current step."
    exit 0
  fi

  if (( claude_exit != 0 )); then
    echo
    echo "Claude exited with status $claude_exit."
    exit 1
  fi

  after_count=$(todo_count)

  if (( after_count == 0 )); then
    echo
    echo "All tasks complete."
    exit 0
  fi

  if [[ -f "$DONE_DIR/$(basename "$before_task")" ]] && [[ ! -f "$before_task" ]]; then
    echo
    echo "Progress: $(basename "$before_task") -> done. $after_count task(s) remaining."
    continue
  fi

  echo
  echo "No progress on $(basename "$before_task") this run."
  echo "Still $after_count task(s) remaining. Stopping."
  exit 1
done

echo
echo "Reached maximum of $MAX_STEPS Claude runs."
echo "$(todo_count) task(s) remain."
exit 1
