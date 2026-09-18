#!/usr/bin/env bash

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASKS_DIR="$ROOT_DIR/tasks"
TODO_DIR="$TASKS_DIR/todo"
STOP_FILE="$ROOT_DIR/.stop_code"
MAX_STEPS="${MAX_STEPS:-50}"
FORMAT_FILTER="$ROOT_DIR/bin/format_stream.jq"
WORKTREES_DIR="$ROOT_DIR/agent-worktree"
MASTER_LOCK="$ROOT_DIR/.master-lock"
LOG_DIR="$ROOT_DIR/logs-code"
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
RUN_DIR="$LOG_DIR/$RUN_ID"
RUNS_LOG="$LOG_DIR/runs.log"

mkdir -p "$RUN_DIR" "$WORKTREES_DIR"
rm -f "$STOP_FILE"

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

todo_files() {
  find "$TODO_DIR" -maxdepth 1 -type f -name '*.md' | sort
}

todo_count() {
  todo_files | wc -l | tr -d ' '
}

log_event() {
  printf '%s  [%s]  %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$RUN_ID" "$*" >> "$RUNS_LOG"
}

with_master_lock() {
  local waited=0
  until mkdir "$MASTER_LOCK" 2>/dev/null; do
    sleep 1
    if (( ++waited > 600 )); then
      echo -e "\033[31mTimed out waiting for $MASTER_LOCK; remove it if no other run is alive.${RESET}" >&2
      return 1
    fi
  done
  "$@"
  local status=$?
  rmdir "$MASTER_LOCK"
  return "$status"
}

branch_exists() {
  git -C "$ROOT_DIR" show-ref --verify --quiet "refs/heads/$1"
}

ensure_task_committed() {
  local task_file="$1" task_name="$2"
  [[ -z "$(git -C "$ROOT_DIR" status --porcelain -- "$task_file")" ]] && return 0
  log_event "committing $task_name to master"
  git -C "$ROOT_DIR" add -- "$task_file" &&
    git -C "$ROOT_DIR" commit --quiet -m "Add milestone ${task_name%.md}" -- "$task_file"
}

claim_worktree() {
  local branch="$1" dir="$WORKTREES_DIR/$1"
  [[ -d "$dir" ]] && return 1
  if branch_exists "$branch"; then
    if git -C "$ROOT_DIR" merge-base --is-ancestor "$branch" master; then
      git -C "$ROOT_DIR" branch -D "$branch" >/dev/null 2>&1
      git -C "$ROOT_DIR" worktree add -b "$branch" "$dir" master >/dev/null 2>&1
    else
      log_event "resuming existing branch $branch"
      git -C "$ROOT_DIR" worktree add "$dir" "$branch" >/dev/null 2>&1
    fi
  else
    git -C "$ROOT_DIR" worktree add -b "$branch" "$dir" master >/dev/null 2>&1
  fi
}

claim_next_task() {
  local task branch
  while read -r task; do
    branch="$(basename "${task%.md}")"
    [[ -d "$WORKTREES_DIR/$branch" ]] && continue
    with_master_lock ensure_task_committed "$task" "$(basename "$task")" || continue
    if claim_worktree "$branch"; then
      printf '%s\n' "$task"
      return 0
    fi
  done < <(todo_files)
  return 1
}

remove_worktree() {
  local dir="$1"
  git -C "$ROOT_DIR" worktree remove --force "$dir" 2>/dev/null || rm -rf "$dir"
}

merge_task() {
  local branch="$1" dir="$2"
  if ! git -C "$dir" rebase master >/dev/null 2>&1; then
    git -C "$dir" rebase --abort 2>/dev/null
    return 1
  fi
  git -C "$ROOT_DIR" merge --ff-only "$branch" >/dev/null
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
  local session="$1" work_dir="$2" prompt_file="$3" step_dir="$4"
  local exit_file done_channel
  exit_file="$(mktemp)"
  done_channel="${session}_done_$$_${RANDOM}"

  tmux kill-session -t "$session" 2>/dev/null || true

  tmux new-session -d -s "$session" -c "$work_dir" -x 220 -y 50 bash -c \
    "claude -p \"\$(cat '$prompt_file')\" --dangerously-skip-permissions --disallowedTools AskUserQuestion --verbose --output-format stream-json | tee '$step_dir/stream.jsonl' | jq -r -f '$FORMAT_FILTER' | tee '$step_dir/console.log'; echo \${PIPESTATUS[0]} > '$exit_file'; tmux wait-for -S '$done_channel'"

  tmux wait-for "$done_channel"

  local exit_code
  exit_code="$(cat "$exit_file" 2>/dev/null)"

  rm -f "$exit_file"

  tmux has-session -t "$session" 2>/dev/null && tmux kill-session -t "$session"

  if [[ ! "$exit_code" =~ ^[0-9]+$ ]]; then
    exit_code=1
  fi

  return "$exit_code"
}

cd "$ROOT_DIR"

log_event "run start  max_steps=$MAX_STEPS  todo=$(todo_count)"

for ((step = 1; step <= MAX_STEPS; step++)); do
  if [[ -f "$STOP_FILE" ]]; then
    echo
    echo -e "${YELLOW}Stop requested via .stop_code.${RESET}"
    log_event "run end    stopped via .stop_code"
    exit 0
  fi

  before_count=$(todo_count)

  if (( before_count == 0 )); then
    echo
    echo -e "${BOLD}All tasks complete.${RESET}"
    log_event "run end    all tasks complete"
    exit 0
  fi

  before_task="$(claim_next_task)"
  if [[ -z "$before_task" ]]; then
    echo
    echo -e "${BOLD}$before_count task(s) remain, all claimed by other agents.${RESET}"
    log_event "run end    remaining tasks claimed elsewhere"
    exit 0
  fi

  task_name="$(basename "$before_task")"
  branch_name="${task_name%.md}"
  worktree_dir="$WORKTREES_DIR/$branch_name"
  session="claude-pico-$branch_name"
  step_dir="$RUN_DIR/$(printf '%02d' "$step")-$branch_name"
  mkdir -p "$step_dir"

  echo
  box_border "┌" "┐"
  box_line "STEP $step/$MAX_STEPS  ·  $before_count task(s) remaining" \
    "${BOLD}STEP $step/$MAX_STEPS${RESET}  ${DIM}·${RESET}  ${before_count} task(s) remaining"
  box_line "next: $task_name" "${DIM}next:${RESET} ${YELLOW}$task_name${RESET}"
  box_line "watch live: make code_attach TASK=$branch_name" \
    "${DIM}watch live:${RESET} ${BOLD}make code_attach TASK=$branch_name${RESET}"
  box_border "└" "┘"
  echo

  log_event "step $step  task=$task_name  branch=$branch_name  log=${step_dir#$LOG_DIR/}"

  ln -sf "$ROOT_DIR/.env" "$worktree_dir/.env"

  if [[ ! -f "$worktree_dir/tasks/todo/$task_name" ]]; then
    echo
    echo -e "\033[31m$task_name is missing from the worktree despite being committed.${RESET}"
    log_event "step $step  FAILED task missing from worktree"
    exit 1
  fi

  {
    cat "$worktree_dir/tasks/PROMPT.md"
    printf '\nYour assigned milestone is `tasks/todo/%s`.\n' "$task_name"
  } > "$step_dir/prompt.md"

  started_at=$SECONDS
  run_claude_in_tmux "$session" "$worktree_dir" "$step_dir/prompt.md" "$step_dir"
  claude_exit=$?
  write_step_summary "$step_dir" "$task_name" "$branch_name" "$claude_exit" "$(( SECONDS - started_at ))"

  if (( claude_exit != 0 )); then
    echo
    echo -e "\033[31mClaude exited with status $claude_exit.${RESET} Worktree left at $worktree_dir for inspection."
    log_event "step $step  FAILED claude exit=$claude_exit"
    exit 1
  fi

  if [[ -f "$worktree_dir/tasks/done/$task_name" ]] && [[ ! -f "$worktree_dir/tasks/todo/$task_name" ]]; then
    if ! with_master_lock merge_task "$branch_name" "$worktree_dir"; then
      echo
      echo -e "\033[31mFailed to merge $branch_name into master.${RESET} Worktree left at $worktree_dir for inspection."
      log_event "step $step  FAILED merge of $branch_name"
      exit 1
    fi

    remove_worktree "$worktree_dir"

    if [[ -f "$before_task" ]]; then
      echo
      echo -e "\033[31m$task_name still exists in tasks/todo after the merge; it would loop forever.${RESET}"
      echo "The merge must remove it — check that the agent committed a git mv to done."
      log_event "step $step  FAILED $task_name survived merge"
      exit 1
    fi

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
  echo "Worktree left at $worktree_dir for inspection. Branch: $branch_name"
  log_event "step $step  NO PROGRESS $task_name  worktree kept at $worktree_dir"
  exit 1
done

echo
echo "Reached maximum of $MAX_STEPS Claude runs."
echo "$(todo_count) task(s) remain."
log_event "run end    reached max steps  $(todo_count) task(s) remain"
exit 1
