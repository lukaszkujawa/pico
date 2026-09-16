# Fact Recall

`VISION.md` promises: "Context may hide information, but important information must remain addressable and recoverable." Today only the first half is built. When the budget is tight, `render_messages` (`src/pico/core/context.py`) collapses a tool result to `[fact N truncated — ...]` plus a 200-char preview — and that content is gone from the model's world. There is no way to get it back: the model can *cite* fact N in its `answer` while being structurally unable to *read* it. For a small model doing a long task, this is exactly the failure the vision exists to prevent — the runtime hid state and offered no handle back to it.

This milestone closes the loop with a `read_fact` tool: given a fact id (`0019`'s stable seq-based ids — this milestone depends on it), return the full stored content from the session log. It also fixes a wart that `read_fact` would otherwise inherit: tools currently cannot signal failure. `ReadFile`'s "could not read ..." string comes back through `ToolRegistry.execute` as an ordinary success (`src/pico/core/loop.py` sets `is_error=True` only for `UnknownToolError`), so error strings become citable facts in the ledger and never feed `tool_failure_streak` (`src/pico/core/stuckness.py`). A `read_fact` miss must not become a "fact", so tool-level errors get a real channel first.

## Design decisions

* **`ToolError` for genuine tool failure.** New exception in `src/pico/core/errors.py`. Tools raise it; `ToolRegistry.execute` lets it propagate; `tool_call_step` catches it alongside `UnknownToolError` and records the message with `is_error=True`. Raising it: `ReadFile`/`WriteFile` on `OSError`, `Shell` on timeout, `read_fact` on an unknown id. A non-zero shell exit code stays a normal result — "exit code 1" from a grep with no matches is information, not failure.
* **`ToolError` does not feed `invalid_action_attempts`.** That counter (`MAX_INVALID_ACTION_ATTEMPTS`) exists for protocol mistakes — malformed arguments, unknown tools — where the model is misusing the interface. A missing file is a legitimate discovery. In `tool_call_step`, increment the counter only for `UnknownToolError`/`InvalidActionError` paths; `ToolError` results still set `is_error=True`, so they are excluded from `facts()` and counted by the existing failure-streak stuckness signal, which is the right pressure.
* **`read_fact` closes over the session.** A factory `fact_recall_tool(session: Session) -> Tool` in `src/pico/core/actions.py`, since the tool needs the log and the existing action dataclasses are deliberately session-free. `register_actions` gains a `session` parameter and registers it (call-site: `run_pico` in `src/pico/app.py`). `register_delegate_actions` registers it too, closed over the *child* session — a delegate can re-read its own truncated evidence, and gets a clean "unknown fact" error for parent ids rather than silently seeing parent state.
* **A recalled fact is an ordinary event.** The `read_fact` result is recorded like any tool call and becomes a fact itself (source `read_fact`). Deduplicating or aliasing it back to the original id would add machinery for no failure it prevents; if the copy is later truncated, it is recoverable by its own id like anything else.
* **Handles advertise the way back.** `render_tool_result`'s summary becomes actionable: `[fact 12 truncated — 5400 chars, ~1350 tokens — call read_fact(12) for the full content] <preview>`. The model should never have to know the recovery mechanism a priori; the handle teaches it at the moment it matters.

## [ ] T001 `ToolError` and honest error recording

### Description

Add `ToolError` to `src/pico/core/errors.py`. Convert the failure returns in `src/pico/core/actions.py` (`ReadFile`/`WriteFile` `OSError` handlers, `Shell` timeout) to raises. In `src/pico/core/loop.py`, catch `ToolError` in `tool_call_step`'s registry branch, record with `is_error=True`, and restrict `invalid_action_attempts` increments to the protocol-mistake paths per the design decisions.

### Acceptance criteria

* `tests/core/test_actions.py` covers: reading a missing file raises `ToolError`; a timed-out shell command raises `ToolError`; a non-zero exit still returns the `exit code N` string as a normal result.
* `tests/core/test_loop.py` covers: a `ToolError` from a tool is recorded with `is_error=True` and excluded from `facts()`; five consecutive `ToolError` failures do **not** end the run via `MAX_INVALID_ACTION_ATTEMPTS` (the stuckness thresholds still apply); five protocol mistakes still do.
* `tests/core/test_stuckness.py` (if needed) confirms `tool_failure_streak` now counts these failures.
* Fully annotated, passes strict Pyright.

## [ ] T002 `read_fact` tool

### Description

In `src/pico/core/actions.py`: add the `read_fact` `ToolSpec` (one required integer parameter, `id`; description telling the model this recovers the full content of a truncated fact) and `fact_recall_tool(session)` returning a `Tool` that looks the id up via `facts(session)` and returns the stored content, raising `ToolError` for an id that matches no fact. Thread the `session` parameter through `register_actions` and `register_delegate_actions` and update their call sites (`src/pico/app.py`, `_run_delegate` in `src/pico/core/loop.py`).

### Acceptance criteria

* `tests/core/test_actions.py` covers: `read_fact` returns the exact original content for a valid id; an unknown id raises `ToolError` naming the id; a non-integer `id` argument produces the standard invalid-field error.
* `tests/core/test_loop.py` covers a scripted end-to-end shape: a large tool result gets truncated to a handle by the renderer, the model calls `read_fact` with the id from the handle, receives the full content, and answers citing the original fact — all against a recording LLM client asserting the handle text (with its `read_fact` hint) is what the model actually saw.
* Fully annotated, passes strict Pyright.

## [ ] T003 Handles that teach recovery

### Description

In `src/pico/core/context.py`: extend the `render_tool_result` handle summary to include the `call read_fact(N) for the full content` hint per the design decisions. Keep the preview and the char/token counts.

### Acceptance criteria

* `tests/core/test_context.py` covers: the handle text names the same id in both the summary and the hint; full-level rendering is unchanged.
* Fully annotated, passes strict Pyright.

## [ ] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` re-exports (`ToolError`, `fact_recall_tool`). Manually verify against a live model if one is configured: ask a question whose tool output overflows the budget and watch the model recover it via `read_fact`.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
