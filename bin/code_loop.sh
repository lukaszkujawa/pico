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

BOLD="\033[1m"
DIM="\033[2m"
CYAN="\033[36m"
YELLOW="\033[33m"
RESET="\033[0m"

BOX_WIDTH=61

box_line() {
  local visible="$1" styled="$2"
  local pad=$(( BOX_WIDTH - 2 - ${#visible} ))
  (( pad < 0 )) && pad=0
  printf "${BOLD}${CYAN}│${RESET} %b%*s ${BOLD}${CYAN}│${RESET}\n" "$styled" "$pad" ""
}

box_border() {
  local corner_left="$1" corner_right="$2"
  printf "${BOLD}${CYAN}%s" "$corner_left"
  printf '─%.0s' $(seq 1 "$BOX_WIDTH")
  printf "%s${RESET}\n" "$corner_right"
}

next_task() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | sort | head -n 1
}

todo_count() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' '
}

run_claude_in_tmux() {
  local exit_file done_channel
  exit_file="$(mktemp)"
  done_channel="${SESSION}_done_$$_${RANDOM}"

  tmux kill-session -t "$SESSION" 2>/dev/null || true

  tmux new-session -d -s "$SESSION" -x 220 -y 50 bash -c \
    "claude -p \"\$(cat '$PROMPT_FILE')\" --dangerously-skip-permissions --disallowedTools AskUserQuestion --verbose --output-format stream-json | jq -r -f '$FORMAT_FILTER'; echo \${PIPESTATUS[0]} > '$exit_file'; tmux wait-for -S '$done_channel'"

  tmux wait-for "$done_channel"

  local exit_code
  exit_code="$(cat "$exit_file" 2>/dev/null)"

  rm -f "$exit_file"

  tmux has-session -t "$SESSION" 2>/dev/null && tmux kill-session -t "$SESSION"

  if [[ ! "$exit_code" =~ ^[0-9]+$ ]]; then
    exit_code=1
  fi

  return "$exit_code"
}

cd "$ROOT_DIR"

for ((step = 1; step <= MAX_STEPS; step++)); do
  before_count=$(todo_count)

  if (( before_count == 0 )); then
    echo
    echo -e "${BOLD}All tasks complete.${RESET}"
    exit 0
  fi

  before_task=$(next_task)
  task_name="$(basename "$before_task")"
  step_line="STEP $step/$MAX_STEPS  ·  $before_count task(s) remaining"
  next_line="next: $task_name"
  attach_line="watch live: make cloude_attach"

  echo
  box_border "┌" "┐"
  box_line "$step_line" "${BOLD}STEP $step/$MAX_STEPS${RESET}  ${DIM}·${RESET}  ${before_count} task(s) remaining"
  box_line "$next_line" "${DIM}next:${RESET} ${YELLOW}$task_name${RESET}"
  box_line "$attach_line" "${DIM}watch live:${RESET} ${BOLD}make cloude_attach${RESET}"
  box_border "└" "┘"
  echo

  run_claude_in_tmux
  claude_exit=$?

  if [[ -f "$STOP_FILE" ]]; then
    rm -f "$STOP_FILE"
    echo
    echo -e "${YELLOW}Stop requested via .stop_code.${RESET} Stopping after current step."
    exit 0
  fi

  if (( claude_exit != 0 )); then
    echo
    echo -e "\033[31mClaude exited with status $claude_exit.${RESET}"
    exit 1
  fi

  after_count=$(todo_count)

  if (( after_count == 0 )); then
    echo
    echo -e "${BOLD}All tasks complete.${RESET}"
    exit 0
  fi

  if [[ -f "$DONE_DIR/$(basename "$before_task")" ]] && [[ ! -f "$before_task" ]]; then
    echo
    echo -e "\033[32m✓${RESET} $(basename "$before_task") ${DIM}->${RESET} done. $after_count task(s) remaining."
    continue
  fi

  echo
  echo -e "\033[31mNo progress on $(basename "$before_task") this run.${RESET}"
  echo "Still $after_count task(s) remaining. Stopping."
  exit 1
done

echo
echo "Reached maximum of $MAX_STEPS Claude runs."
echo "$(todo_count) task(s) remain."
exit 1
