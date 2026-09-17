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
LOG_DIR="$ROOT_DIR/logs-code"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$LOG_DIR/$RUN_ID"
RUNS_LOG="$LOG_DIR/runs.log"

mkdir -p "$RUN_DIR"

export UV_PROJECT_ENVIRONMENT="$ROOT_DIR/.venv"
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

log_event() {
  printf '%s  %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$*" >> "$RUNS_LOG"
}

write_step_summary() {
  local step_dir="$1" task="$2" branch="$3" exit_code="$4" duration="$5"
  local result

  result="$(jq -c -R 'fromjson? | select(.type == "result")' "$step_dir/stream.jsonl" 2>/dev/null | tail -n 1)"

  {
    printf 'task:      %s\n' "$task"
    printf 'branch:    %s\n' "$branch"
    printf 'finished:  %s\n' "$(date +%Y-%m-%dT%H:%M:%S)"
    printf 'duration:  %ss\n' "$duration"
    printf 'exit:      %s\n' "$exit_code"
    if [[ -n "$result" ]]; then
      jq -r '
        "cost:      $" + (.total_cost_usd // 0 | tostring),
        "turns:     " + (.num_turns // 0 | tostring),
        "session:   " + (.session_id // "-"),
        "error:     " + (if .is_error then (.subtype // "yes") else "no" end)
      ' <<< "$result"
    else
      printf 'cost:      unknown (no result event)\n'
    fi
  } > "$step_dir/summary.txt"
}

run_claude_in_tmux() {
  local work_dir="$1" prompt_file="$2" step_dir="$3"
  local exit_file done_channel
  exit_file="$(mktemp)"
  done_channel="${SESSION}_done_$$_${RANDOM}"

  tmux kill-session -t "$SESSION" 2>/dev/null || true

  tmux new-session -d -s "$SESSION" -c "$work_dir" -x 220 -y 50 bash -c \
    "claude -p \"\$(cat '$prompt_file')\" --dangerously-skip-permissions --disallowedTools AskUserQuestion --verbose --output-format stream-json | tee '$step_dir/stream.jsonl' | jq -r -f '$FORMAT_FILTER' | tee '$step_dir/console.log'; echo \${PIPESTATUS[0]} > '$exit_file'; tmux wait-for -S '$done_channel'"

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

  if [[ -d "$WORKTREE_DIR" ]]; then
    local current_branch
    current_branch="$(git -C "$WORKTREE_DIR" branch --show-current 2>/dev/null)"
    if [[ "$current_branch" == "$branch_name" ]]; then
      echo -e "${DIM}Resuming existing worktree on $branch_name.${RESET}"
      return 0
    fi
    if [[ -n "$(git -C "$WORKTREE_DIR" status --porcelain 2>/dev/null)" ]]; then
      echo -e "\033[31mWorktree at $WORKTREE_DIR is on '$current_branch' with uncommitted changes; refusing to remove it.${RESET}"
      return 1
    fi
  fi

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

log_event "run start  id=$RUN_ID  max_steps=$MAX_STEPS  todo=$(todo_count)"

for ((step = 1; step <= MAX_STEPS; step++)); do
  before_count=$(todo_count)

  if (( before_count == 0 )); then
    echo
    echo -e "${BOLD}All tasks complete.${RESET}"
    log_event "run end    all tasks complete"
    exit 0
  fi

  before_task=$(next_task)
  task_name="$(basename "$before_task")"
  step_line="STEP $step/$MAX_STEPS  ·  $before_count task(s) remaining"
  next_line="next: $task_name"
  attach_line="watch live: make code_attach"

  branch_name="${task_name%.md}"
  step_dir="$RUN_DIR/$(printf '%02d' "$step")-$branch_name"
  mkdir -p "$step_dir"

  echo
  box_border "┌" "┐"
  box_line "$step_line" "${BOLD}STEP $step/$MAX_STEPS${RESET}  ${DIM}·${RESET}  ${before_count} task(s) remaining"
  box_line "$next_line" "${DIM}next:${RESET} ${YELLOW}$task_name${RESET}"
  box_line "$attach_line" "${DIM}watch live:${RESET} ${BOLD}make code_attach${RESET}"
  box_border "└" "┘"
  echo

  log_event "step $step  task=$task_name  branch=$branch_name  log=${step_dir#$LOG_DIR/}"

  if ! prepare_worktree "$branch_name"; then
    echo
    echo -e "\033[31mFailed to create worktree for $branch_name.${RESET}"
    log_event "step $step  FAILED worktree creation"
    exit 1
  fi

  ln -sf "$ROOT_DIR/.env" "$WORKTREE_DIR/.env"

  mkdir -p "$WORKTREE_DIR/tasks/todo"
  if [[ ! -f "$WORKTREE_DIR/tasks/todo/$task_name" ]]; then
    cp "$before_task" "$WORKTREE_DIR/tasks/todo/$task_name"
  fi

  started_at=$SECONDS
  run_claude_in_tmux "$WORKTREE_DIR" "$WORKTREE_DIR/tasks/PROMPT.md" "$step_dir"
  claude_exit=$?
  write_step_summary "$step_dir" "$task_name" "$branch_name" "$claude_exit" "$(( SECONDS - started_at ))"

  if [[ -f "$STOP_FILE" ]]; then
    rm -f "$STOP_FILE"
    echo
    echo -e "${YELLOW}Stop requested via .stop_code.${RESET} Stopping after current step."
    log_event "run end    stopped via .stop_code"
    exit 0
  fi

  if (( claude_exit != 0 )); then
    echo
    echo -e "\033[31mClaude exited with status $claude_exit.${RESET} Worktree left at $WORKTREE_DIR for inspection."
    log_event "step $step  FAILED claude exit=$claude_exit"
    exit 1
  fi

  worktree_done_file="$WORKTREE_DIR/tasks/done/$task_name"
  worktree_todo_file="$WORKTREE_DIR/tasks/todo/$task_name"

  if [[ -f "$worktree_done_file" ]] && [[ ! -f "$worktree_todo_file" ]]; then
    if ! git -C "$ROOT_DIR" merge --ff-only "$branch_name" >/dev/null; then
      echo
      echo -e "\033[31mFailed to fast-forward master to $branch_name.${RESET} Worktree left at $WORKTREE_DIR for inspection."
      log_event "step $step  FAILED fast-forward merge of $branch_name"
      exit 1
    fi

    remove_worktree

    after_count=$(todo_count)
    log_event "step $step  DONE $task_name  merged  $after_count task(s) remaining"

    if (( after_count == 0 )); then
      echo
      echo -e "${BOLD}All tasks complete.${RESET}"
      log_event "run end    all tasks complete"
      exit 0
    fi

    echo
    echo -e "\033[32m✓${RESET} $task_name ${DIM}->${RESET} done. $after_count task(s) remaining."
    continue
  fi

  echo
  echo -e "\033[31mNo progress on $task_name this run.${RESET}"
  echo "Worktree left at $WORKTREE_DIR for inspection. Branch: $branch_name"
  log_event "step $step  NO PROGRESS $task_name  worktree kept at $WORKTREE_DIR"
  exit 1
done

echo
echo "Reached maximum of $MAX_STEPS Claude runs."
echo "$(todo_count) task(s) remain."
log_event "run end    reached max steps  $(todo_count) task(s) remain"
exit 1
