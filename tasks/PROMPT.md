Before doing anything else, read `VISION.md`. It defines the project goals, architecture principles, and coding guidelines. Follow it throughout the run.

You are working unattended on the Pico project.

Milestones live in `tasks/todo/`, one markdown file per milestone, named `NNNN-description.md`. The numeric prefix defines execution order.

1. Pick the file in `tasks/todo/` with the lowest `NNNN`. Work only on that milestone.
2. Read the entire milestone before making changes.
3. Complete its tasks in order, top to bottom.
4. After completing each task, change its checkbox from `[ ]` to `[X]`. Do not skip or reorder tasks.
5. Make the smallest complete implementation required. Avoid speculative abstractions, unrelated cleanup, and scope expansion.
6. Once every task is `[X]`, run `make check` and fix all failures. Do not proceed while it is red.
7. When `make check` passes, `git mv` the milestone file from `tasks/todo/` to `tasks/done/`.
8. Commit the implementation, tests, completed milestone file, and move in one commit with a concise, meaningful message.

Never move an incomplete milestone to `tasks/done/` or commit with a failing `make check`.

For unspecified design decisions, choose the simplest option consistent with `VISION.md` and the existing codebase.

You are running completely unattended. Never ask for clarification, confirmation, approval, or input. If blocked by something that cannot be resolved from the repository, stop without marking the affected task complete.
