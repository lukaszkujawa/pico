Before doing anything else, read `VISION.md`. It defines the project goals, architecture principles, and coding guidelines. Follow it throughout the run.

You are working unattended on the Pico project.

Milestones live in `tasks/todo/`, one markdown file per milestone, named `NNNN-description.md`. The numeric prefix defines execution order.

1. Pick the file in `tasks/todo/` with the lowest `NNNN`. Work only on that milestone.
2. Read the entire milestone before making changes.
3. Complete its tasks in order, top to bottom.
4. After completing each task, run `make check` and fix all failures before moving to the next task. This is the only command you need to verify your work — do not run `uv run pytest`, `uv run ruff`, `uv run pyright`, or any other check individually; `make check` already runs all of them. Never run `make evals`: it needs a live model and long wall-clock time and is run by a human separately. If a milestone tells you to run it, skip that instruction and note in the completion notes that evals were left to a manual run.
5. Once a task's `make check` is green, change its checkbox from `[ ]` to `[X]`. Do not skip or reorder tasks.
6. Make the smallest complete implementation required. Avoid speculative abstractions, unrelated cleanup, and scope expansion.
7. When every task is `[X]` and `make check` is green, `git mv` the milestone file from `tasks/todo/` to `tasks/done/`.
8. Commit the implementation, tests, completed milestone file, and move in one commit with a concise, meaningful message.

Never move an incomplete milestone to `tasks/done/` or commit with a failing `make check`.

For unspecified design decisions, choose the simplest option consistent with `VISION.md` and the existing codebase.

You are running completely unattended. Never ask for clarification, confirmation, approval, or input. If blocked by something that cannot be resolved from the repository, stop without marking the affected task complete.

You are running in one-shot mode: the process terminates the moment your final response ends, and any background jobs die with it. Never start a command in the background and end your turn expecting to be re-invoked when it finishes — that notification will never come and the run will be counted as failed. Run long commands in the foreground with a sufficient timeout; if a command must run in the background, keep waiting on it with further tool calls in the same turn and only finish after it has completed and you have committed its results.
