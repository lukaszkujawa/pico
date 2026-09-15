# Pico

Pico is a robust agentic harness designed for local and smaller language models, such as Qwen 3.8 27B.

Its goal is to let modest models complete complex, long-running tasks with reliability approaching frontier models.

Pico should achieve this through a strong runtime rather than relying on model scale.

The model should reason and make decisions.

The runtime should remember, organise, compute, verify, and manage context.

The central principle is:

> Context is a cache, not memory.

Important state must never exist only inside the model's context.

Pico should compensate for limited context and weaker long-horizon reasoning through a few core ideas:

* **Compiled context.** Every model call receives the smallest useful view of current state rather than an ever-growing conversation.
* **Handles instead of payloads.** Large files, tool results, tables, logs, and other data remain outside the prompt and are referenced by stable handles.
* **Structured state.** Durable facts, goals, plans, results, and provenance live in software-managed state that can be queried directly.
* **Deterministic computation.** SQL and ordinary code handle filtering, aggregation, bookkeeping, validation, and other work that does not require model judgement.
* **Tunnel vision.** Each model invocation should see only what it needs for the decision immediately in front of it.
* **Recursive delegation.** Complex work can be decomposed into isolated subagents with small, explicit contexts and typed results.
* **Verification.** Results should be checked by deterministic software wherever possible rather than trusted because a model produced them.
* **Recoverability.** Context may hide information, but important information must remain addressable and recoverable.

Pico should feel simple to use even when the machinery underneath is rigorous.

A user should be able to give it a difficult task and let it work through that task methodically without losing the goal, drowning in context, or forgetting where information came from.

Complexity in the runtime is justified only when it measurably improves the ability of smaller models to solve harder tasks.

## How

Pico should be built with clean, restrained, readable Python.

Optimise code for readability and beautiful design.

### Keep it simple

Less code is better.

Prefer the smallest complete implementation.

Do not build abstractions for hypothetical future requirements.

Do not create interfaces for one implementation.

Do not introduce factories, managers, services, helpers, base classes, or frameworks without a clear domain reason.

Prefer domain names such as:

`Session`, `Artifact`, `Context`, `Ledger`, `Plan`, `Job`, `Fact`, `Tool`, `Run`.

Use classes when they represent meaningful objects with behaviour. Use functions when a class adds nothing.

Prefer composition to inheritance.

Prefer shallow call stacks and obvious control flow.

Prefer the standard library and a small number of well-chosen dependencies.

### Make correctness mechanical

All code must be type safe.

Static type checking runs in strict mode.

All behaviour must have unit test coverage. Bugs require regression tests.

Tests should verify behaviour and invariants, not implementation details.

Deterministic checks are preferred over model judgement.

The repository must remain green under `make check`.

A change is not complete because an agent believes it works. It is complete because the tests, type checker, linter, and required verification pass.

### Make code explain itself

Python code should contain no explanatory comments.

Do not narrate code with comments.

Do not add docstrings by default.

Names, structure, types, and tests should make the implementation understandable.

If code requires a paragraph explaining what it does, first try to make the code clearer.

### Keep architecture boring

Prefer direct solutions.

Prefer synchronous code unless concurrency is genuinely required.

Prefer explicit data and ordinary control flow over framework machinery.

Keep modules cohesive and the repository small.

Delete dead code immediately.

Refactor towards fewer concepts, fewer abstractions, and clearer names.

The architecture should be easy to explain.

The code should be pleasant to read.

The runtime should be more reliable than the model using it.

## Environment

Pico uses `uv` to manage Python and dependencies, and `pyproject.toml` as the single source of truth for them.

Never edit dependency lists in `pyproject.toml` by hand and never call `pip` directly.

Add a runtime dependency with `uv add <package>`. Add a dev-only dependency (tooling, test libraries) with `uv add --dev <package>`. Remove one with `uv remove [--dev] <package>`.

Run any Python tool or script through `uv run`, for example `uv run pytest` or `uv run python -m pico`, so it executes inside the project's managed environment.

Before finishing any change, run `make check`. It runs Ruff (lint and format check), Pyright in strict mode, and pytest with coverage. All of it must pass.

Use `make format` to auto-fix formatting and lint issues before resorting to manual fixes.