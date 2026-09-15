#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="$ROOT_DIR/tasks"
TODO_DIR="$TASKS_DIR/todo"
DONE_DIR="$TASKS_DIR/done"
PROMPT_FILE="$TASKS_DIR/PROMPT.md"
MAX_STEPS="${MAX_STEPS:-50}"

next_task() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | sort | head -n 1
}

todo_count() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' '
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
  echo "============================================================"
  echo

  claude -p "$(cat "$PROMPT_FILE")" \
    --dangerously-skip-permissions \
    --disallowedTools "AskUserQuestion" \
    --verbose

  claude_exit=${PIPESTATUS[0]}

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
