#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="$ROOT_DIR/tasks"
TODO_DIR="$TASKS_DIR/todo"
STOP_FILE="$ROOT_DIR/.stop_code"
MAX_STEPS="${MAX_STEPS:-50}"
SESSION="claude-pico"
FORMAT_FILTER="$ROOT_DIR/bin/format_stream.jq"
WORKTREE_DIR="$ROOT_DIR/agent-worktree"

source "$ROOT_DIR/.venv/bin/activate"

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
  local work_dir="$1" prompt_file="$2"
  local exit_file done_channel
  exit_file="$(mktemp)"
  done_channel="${SESSION}_done_$$_${RANDOM}"

  tmux kill-session -t "$SESSION" 2>/dev/null || true

  tmux new-session -d -s "$SESSION" -c "$work_dir" -x 220 -y 50 bash -c \
    "claude -p \"\$(cat '$prompt_file')\" --dangerously-skip-permissions --disallowedTools AskUserQuestion --verbose --output-format stream-json | jq -r -f '$FORMAT_FILTER'; echo \${PIPESTATUS[0]} > '$exit_file'; tmux wait-for -S '$done_channel'"

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

remove_worktree() {
  git -C "$ROOT_DIR" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || rm -rf "$WORKTREE_DIR"
}

branch_exists() {
  git -C "$ROOT_DIR" show-ref --verify --quiet "refs/heads/$1"
}

prepare_worktree() {
  local branch_name="$1"

  remove_worktree

  if branch_exists "$branch_name"; then
    if git -C "$ROOT_DIR" merge-base --is-ancestor "$branch_name" master; then
      git -C "$ROOT_DIR" branch -D "$branch_name" >/dev/null &&
        git -C "$ROOT_DIR" worktree add -b "$branch_name" "$WORKTREE_DIR" master >/dev/null
    else
      echo -e "${DIM}Resuming existing branch $branch_name.${RESET}"
      git -C "$ROOT_DIR" worktree add "$WORKTREE_DIR" "$branch_name" >/dev/null
    fi
  else
    git -C "$ROOT_DIR" worktree add -b "$branch_name" "$WORKTREE_DIR" master >/dev/null
  fi
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
  attach_line="watch live: make claude_attach"

  branch_name="${task_name%.md}"

  echo
  box_border "┌" "┐"
  box_line "$step_line" "${BOLD}STEP $step/$MAX_STEPS${RESET}  ${DIM}·${RESET}  ${before_count} task(s) remaining"
  box_line "$next_line" "${DIM}next:${RESET} ${YELLOW}$task_name${RESET}"
  box_line "$attach_line" "${DIM}watch live:${RESET} ${BOLD}make cloude_attach${RESET}"
  box_border "└" "┘"
  echo

  if ! prepare_worktree "$branch_name"; then
    echo
    echo -e "\033[31mFailed to create worktree for $branch_name.${RESET}"
    exit 1
  fi

  ln -sf "$ROOT_DIR/.env" "$WORKTREE_DIR/.env"

  run_claude_in_tmux "$WORKTREE_DIR" "$WORKTREE_DIR/tasks/PROMPT.md"
  claude_exit=$?

  if [[ -f "$STOP_FILE" ]]; then
    rm -f "$STOP_FILE"
    echo
    echo -e "${YELLOW}Stop requested via .stop_code.${RESET} Stopping after current step."
    exit 0
  fi

  if (( claude_exit != 0 )); then
    echo
    echo -e "\033[31mClaude exited with status $claude_exit.${RESET} Worktree left at $WORKTREE_DIR for inspection."
    exit 1
  fi

  worktree_done_file="$WORKTREE_DIR/tasks/done/$task_name"
  worktree_todo_file="$WORKTREE_DIR/tasks/todo/$task_name"

  if [[ -f "$worktree_done_file" ]] && [[ ! -f "$worktree_todo_file" ]]; then
    if ! git -C "$ROOT_DIR" merge --ff-only "$branch_name" >/dev/null; then
      echo
      echo -e "\033[31mFailed to fast-forward master to $branch_name.${RESET} Worktree left at $WORKTREE_DIR for inspection."
      exit 1
    fi

    remove_worktree

    after_count=$(todo_count)

    if (( after_count == 0 )); then
      echo
      echo -e "${BOLD}All tasks complete.${RESET}"
      exit 0
    fi

    echo
    echo -e "\033[32m✓${RESET} $task_name ${DIM}->${RESET} done. $after_count task(s) remaining."
    continue
  fi

  echo
  echo -e "\033[31mNo progress on $task_name this run.${RESET}"
  echo "Worktree left at $WORKTREE_DIR for inspection. Branch: $branch_name"
  exit 1
done

echo
echo "Reached maximum of $MAX_STEPS Claude runs."
echo "$(todo_count) task(s) remain."
exit 1
