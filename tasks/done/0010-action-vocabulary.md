# Action Vocabulary

Roadmap milestone 3 of 7 (see `0008-session-store.md` for the full arc).

`0009` gives Pico a declarative loop that dispatches whatever tool calls the model makes through `pico.core.tools.ToolRegistry`, with no validation beyond "does a tool with this name exist." This milestone gives Pico a first real, typed set of actions: `answer`, `read_file`, `write_file`, `shell`. `answer` terminates the run with a final response instead of producing a tool result; the other three are file/shell primitives a small local model needs to do real work.

**Actions vs tools, resolved:** actions are not a parallel system next to `ToolRegistry` — they *are* tools. Each action is a typed, frozen dataclass with a `from_arguments(Mapping[str, object]) -> Self` classmethod that validates and converts raw tool-call arguments (missing/mistyped fields raise `InvalidActionError`), registered into the existing `ToolRegistry` as an ordinary `Tool`. No second dispatch mechanism, no `action.py`-style global registry — `ToolRegistry` stays the one place that maps a name to behavior. `answer` is the one action that isn't a passthrough: its execution needs to make the loop stop instead of feeding a result back for another turn, handled by a small addition to `tool_call_step` (see T004), not a separate code path.

Validation failures are recoverable, not fatal: an invalid action's error is returned as a normal `ToolResult(is_error=True)`, which already flows into `session.messages()` for the model's next turn — that *is* the retry mechanism (the model sees its mistake and tries again next turn), so no separate `parse_action_with_retry` loop is needed. A bounded attempt cap prevents infinite loops when the model can't self-correct (see T005).

## [X] T001 `InvalidActionError` and the `Action` base shape

### Description

In `src/pico/core/actions.py` (new module):

* `InvalidActionError(ValueError)` — raised when raw tool-call arguments fail to validate against an action's required fields/types. Message must name the action and the specific problem (missing field, wrong type), matching the old architecture's style (`agentvm/action.py`'s `InvalidActionError` messages) closely enough to be useful to a small model reading it back as a tool result.
* A `_require(arguments: Mapping[str, object], field: str, expected: type) -> object` helper: raises `InvalidActionError` if `field` is missing from `arguments` or `not isinstance(value, expected)`; otherwise returns the value. This is the one piece of repeated validation logic every action's `from_arguments` uses — keep it a plain function, not a class hierarchy.

Do not define the four concrete actions yet — this task is the shared error type and validation helper only, unit-testable in isolation.

### Acceptance criteria

* `tests/core/test_actions.py` (new) covers: `_require` returns the value when present and correctly typed; raises `InvalidActionError` when the field is missing; raises `InvalidActionError` when the field has the wrong type; error messages name the field.
* Fully annotated, passes strict Pyright.

## [X] T002 `ReadFile`, `WriteFile`, `Shell` actions

### Description

Still in `src/pico/core/actions.py`, define three frozen dataclasses, each with a `from_arguments` classmethod built on T001's `_require`:

* `ReadFile(path: str)` — required: `path: str`.
* `WriteFile(path: str, content: str)` — required: `path: str`, `content: str`.
* `Shell(command: str)` — required: `command: str`. No `cwd`/`timeout` extras from the old vocabulary — start minimal, per the milestone's "scoped down" instruction; these can be added later if a real need shows up.

Each dataclass also gets an `execute(self) -> str` method containing the actual side effect:

* `ReadFile.execute` reads the file at `path` and returns its contents as a string; a missing/unreadable file returns a descriptive error string (caught `OSError` -> message), not a raised exception — tool execution must always produce a string result, matching how `tool_call_step` already treats `ToolRegistry.execute` as returning `str`.
* `WriteFile.execute` writes `content` to `path` (creating parent directories is out of scope — a missing parent directory is a normal `OSError` turned into an error string, same pattern as `ReadFile`), returns a short confirmation string (e.g. `f"wrote {len(content)} bytes to {path}"`).
* `Shell.execute` runs `command` via `subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)` and returns combined stdout+stderr (stdout then stderr if both non-empty), prefixed with an exit-code marker if the command failed (non-zero exit). A `subprocess.TimeoutExpired` becomes an error string, not a raised exception. This is a deliberately blunt, unsandboxed shell action — Pico's execution environment is trusted per `VISION.md`'s scope (no sandboxing infrastructure exists yet and building one is not this milestone's job).

### Acceptance criteria

* `tests/core/test_actions.py` covers: `from_arguments` success and `InvalidActionError` (missing field, wrong type) for all three actions; `ReadFile.execute` reads an existing file and returns an error string for a missing one; `WriteFile.execute` writes a file (verified by reading it back) and returns an error string when the parent directory doesn't exist; `Shell.execute` returns stdout for a successful command, includes an exit-code marker for a failing command, and returns an error string on timeout (use a short timeout override in the test, not the real 30s).
* Fully annotated, passes strict Pyright.

## [X] T003 `Answer` action

### Description

Still in `src/pico/core/actions.py`:

* `Answer(content: str)` — required: `content: str`. No `execute` method returning a string result — `Answer` doesn't produce a tool result, it terminates the run. Instead give it a distinct shape from the other three so `tool_call_step` (T004) can tell it apart: define `Action = ReadFile | WriteFile | Shell | Answer` as the module's union type, and let `tool_call_step` special-case `Answer` by isinstance/match rather than duck-typing an `execute` that returns something meaningless for it.

### Acceptance criteria

* `tests/core/test_actions.py` covers `Answer.from_arguments` success and `InvalidActionError` for a missing `content` field.
* Fully annotated, passes strict Pyright.

## [X] T004 Wire actions into `ToolRegistry` and the loop

### Description

This is the task that makes the vocabulary load-bearing:

* In `src/pico/core/actions.py`, add `ACTIONS: dict[str, type[ReadFile | WriteFile | Shell | Answer]]` mapping `"read_file"`, `"write_file"`, `"shell"`, `"answer"` to their classes, and `register_actions(registry: ToolRegistry) -> None` that builds a `Tool` for each entry: `spec` is a `ToolSpec` with a name/description/JSON-schema `parameters` derived from the action's fields (hand-write each `ToolSpec` next to its action rather than generating schemas reflectively — four actions is few enough that explicit is clearer than a schema-generation helper), and `execute` is a closure that calls `Action.from_arguments(arguments)` then `.execute()`, catching `InvalidActionError` and returning `str(error)` as the tool result (not raising — a raised exception would crash `tool_call_step`, but an `InvalidActionError` is exactly the kind of recoverable-by-retry failure described in the milestone intro, so it must come back as a normal string result the model can see and correct from).
* `answer` is registered as a tool (so it appears in `tools.specs()` and the model can call it like any other action) but is never dispatched through the generic `Tool.execute` closure — `tool_call_step` in `src/pico/core/loop.py` needs to recognize a call named `"answer"` before running it through the registry, validate it via `Answer.from_arguments`, and on success store the validated content (e.g. `runner.final_answer: str | None`, mirroring the existing `runner.pending_tool_calls` transient-state pattern from `0009` T003) then return `"done"` instead of `"continue"` — matching `StepOutcome`'s existing contract, no new outcome value needed. On an invalid `answer` call (bad arguments), treat it like any other invalid action: record a `ToolCallRecorded(is_error=True)` with the validation error and return `"continue"` so the model gets another turn. Publish `ToolCallStarted`/`ToolCallFinished` for the `answer` call the same as any other tool call, for TUI/bus consistency.
* `src/pico/app.py`: call `register_actions(tools)` when building the `ToolRegistry` in `run_pico`, so the running app actually exposes these actions to the model (this is what makes the milestone load-bearing rather than a parallel unused module, matching how `0009` T004 retired `Run` — nothing here may be unreachable from the running app).

### Acceptance criteria

* `tests/core/test_actions.py` covers: `register_actions` populates all four tool names into a `ToolRegistry`'s `specs()`; each non-`answer` tool's `execute` closure round-trips valid arguments to the right `Action.execute()` result; each non-`answer` tool's `execute` closure returns the `InvalidActionError` message as a string (not a raised exception) for invalid arguments.
* `tests/core/test_loop.py` covers: a stream step that yields a valid `answer` tool call causes the iteration to return `"done"`, records a `ToolCallRecorded` (or equivalent — decide and test the exact recorded shape) with the answer content, and publishes `ToolCallStarted`/`ToolCallFinished`; an invalid `answer` call (missing `content`) returns `"continue"` with an error recorded, not `"done"`.
* `tests/test_app.py` confirms `run_pico`'s `ToolRegistry` includes the four actions (e.g. via a lightweight seam, matching however the existing tests already inspect `tools`/`ToolRegistry` construction).
* Fully annotated, passes strict Pyright.

## [X] T005 Bounded retry on repeated invalid actions

### Description

An unbounded sequence of invalid tool calls (model keeps making the same mistake) must not run forever. In `src/pico/core/loop.py`, add a per-run invalid-action counter to `LoopRunner` (e.g. `runner.invalid_action_attempts: int`, reset never — it's a whole-run budget, not per-tool-call), incremented in `tool_call_step` whenever a call's `execute` result is an error (`is_error=True`, covering both `InvalidActionError` and `UnknownToolError` paths — both are "the model tried something that didn't work"). Add `LoopConfig`-independent constant `MAX_INVALID_ACTION_ATTEMPTS = 5` (module-level in `loop.py`, matching the old architecture's `MAX_ACTION_ATTEMPTS` sizing). When the counter reaches the cap mid-run, `tool_call_step` returns `"done"` instead of `"continue"` after recording the final failing call, ending the run rather than looping forever — no new `StepOutcome` variant, no exception type; this reuses the same "done" path `answer` uses, just reached a different way. Do not reset the counter on a successful call *within the same run* — a model that fails twice, succeeds once, then fails five more times has still demonstrated it's stuck; keep the budget simple as a run-lifetime total rather than adding streak-reset logic (streak detection proper is `0014`'s job, not this milestone's).

### Acceptance criteria

* `tests/core/test_loop.py` covers: a run where every tool call is invalid stops after exactly `MAX_INVALID_ACTION_ATTEMPTS` recorded error calls, ending with `"done"` (verify via the resulting `RunFinished` and the count of `ToolCallRecorded(is_error=True)` events in the session) rather than looping until `max_steps`; a run with fewer than the cap's worth of invalid calls interspersed with valid ones completes normally without early termination.
* Fully annotated, passes strict Pyright.

## [X] T006 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` to re-export the new public surface (`Action`, `ReadFile`, `WriteFile`, `Shell`, `Answer`, `InvalidActionError`, `ACTIONS`, `register_actions`, `MAX_INVALID_ACTION_ATTEMPTS`).

### Acceptance criteria

* `make check` passes with no errors.
* `grep -rn "def _step\b" src` returns nothing (confirms no parallel hand-rolled dispatch was reintroduced alongside `ToolRegistry`).
* Manually sanity-check (or note if no live LLM endpoint is reachable in this environment) that `uv run python -m pico` starts with the four actions registered.

### Completion

Commit:
