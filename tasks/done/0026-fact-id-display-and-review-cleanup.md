# Fact ID Display and Review Cleanup

`0019` made `seq` the single, stable identity for a fact — assigned once at append time and shared by the ledger, the context renderer, and citation validation. The TUI never picked this up. `PicoApp._fact_count` (`src/pico/tui/app.py`) is a separate, locally 0-indexed counter that increments only on successful tool-call closes and answer-pane creation; it does not track `seq` and is not derived from it. The moment a session has any event interleaved between tool calls — a user message, an assistant message, a plan event, which is every real session — the `→ fact #N` badge shown in the UI names a different number than the one `read_fact(id)` actually expects. `tests/tui/test_app.py` asserts the counter's own internal sequence (`fact_index == 0`, `== 1`, ...) without ever comparing it to a real `Session`'s `facts()`, so the divergence is invisible to the suite.

Two smaller items surfaced in the same review: `goal()`/`Goal` in `src/pico/core/ledger.py` are defined, exported, and unit-tested but have no caller anywhere in `src/`, which is dead code by `VISION.md`'s own standard. And `src/pico/evals/runner.py`'s `_check` catches every exception from a task's own `check` function and reports it as a model failure, which hides a broken check behind a false-negative eval result.

## Design decisions

* **Carry the real fact id on the bus, not a TUI-local counter.** `ToolCallFinished` (`src/pico/core/events.py`) should carry the fact id the runtime already knows (the event's `seq`, i.e. `Fact.id` from `facts()`) rather than have the TUI invent its own count. `_fact_count` and its increment-on-answer-pane side effect are deleted.
* **`AnswerPaneCreate` does not need a fact id.** Only successful tool calls become facts; the answer pane's own `_fact_count += 1` in `on_answer_pane_create` was never meaningful (nothing reads a fact id off an answer pane) and is removed along with the counter.
* **Delete `goal()`/`Goal` outright.** No deprecation shim, no re-export kept for compatibility — remove the function, the dataclass, their exports in `src/pico/core/__init__.py`, and their tests in `tests/core/test_ledger.py`.
* **`_check` re-raises, it does not swallow.** A task's `check` function is trusted harness code, not untrusted model output; if it throws, that is a bug in the eval suite and should fail loudly, not silently score as "model failed."

## [X] T001 Real fact ids on `ToolCallFinished`

### Description

In `src/pico/core/events.py`, add a `fact_id: int | None` field to `ToolCallFinished`, populated by the runtime at the point the event is published in `src/pico/core/loop.py`'s `tool_call_step` — the fact id for a successful, non-`answer`/non-`delegate` tool call is the `seq` of the `ToolCallRecorded` event about to be appended for it (match the numbering `facts()` in `src/pico/core/ledger.py` already uses). Failed calls and calls that don't produce a fact (`answer`, `delegate`) carry `fact_id=None`.

### Acceptance criteria

* `tests/core/test_loop.py` covers: a successful tool call's published `ToolCallFinished.fact_id` equals the `id` that `facts(session)` reports for that same call after it's appended; a failed tool call's `fact_id` is `None`; an `answer` or `delegate` call's `fact_id` is `None`; a session with a user/assistant message interleaved before a tool call still yields a `fact_id` matching `facts()` (the case the old TUI counter got wrong).
* Fully annotated, passes strict Pyright.

## [X] T002 TUI reads the real fact id

### Description

In `src/pico/tui/messages.py`, thread `fact_id` from `ToolCallFinished` through to `ToolCallPaneClose` (add the field, pass it through in `translate`). In `src/pico/tui/app.py`, delete `_fact_count` entirely: `on_tool_call_pane_close` passes `message.fact_id` straight to `pane.finish(...)`, and `on_answer_pane_create` no longer touches any counter. Reset logic in `action_new_session` drops the `_fact_count = 0` line.

### Acceptance criteria

* `tests/tui/test_app.py`'s existing fact-index tests are rewritten to publish `ToolCallFinished` with an explicit `fact_id` (not an implicit position) and assert the pane shows exactly that id — including a case where the id is non-contiguous (e.g. `fact_id=7` after a `fact_id=2`), which the old zero-up-counter could never produce and could not have passed.
* No reference to `_fact_count` remains anywhere in `src/` or `tests/`.
* Fully annotated, passes strict Pyright.

## [X] T003 Delete dead `goal()`/`Goal`

### Description

Remove `goal()` and `Goal` from `src/pico/core/ledger.py`, their re-exports from `src/pico/core/__init__.py`, and their tests from `tests/core/test_ledger.py`.

### Acceptance criteria

* `grep -rn "\bgoal(\|\bGoal\b" src tests` shows no remaining references.
* `make check` coverage floor still passes without needing new tests to compensate.

## [X] T004 `evals._check` fails loudly on a broken check

### Description

In `src/pico/evals/runner.py`, remove the `try/except Exception: return False` around `task.check(...)` in `_check` — let an exception from a task's own check function propagate instead of being scored as a failed eval.

### Acceptance criteria

* `tests/evals/test_runner.py` covers: a task whose `check` raises propagates that exception out of `run_task`/`run_suite` rather than yielding `passed=False`; a task whose `check` returns normally (`True` or `False`) behaves exactly as before.
* Fully annotated, passes strict Pyright.

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Confirm no `_fact_count`, `Fact.index`, or bare `except Exception` survives in the touched files.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
