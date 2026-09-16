# Delegate Sub-agents

Roadmap milestone 6 of 7 (see `0008-session-store.md` for the full arc).

This is the "recursive agentic tasks" requirement: a `delegate` action (extending `0010`'s vocabulary) that spawns an isolated, constrained inner loop — reusing the declarative loop runner from `0009` with a narrower action set (read-only: no `write_file`/`shell`-that-mutates/`delegate` itself, preventing unbounded recursion) and its own bounded step budget — to answer a single, scoped question and return a typed, evidence-cited result.

Depends on `0009` (the loop runner must already be generic enough to run with a different `LoopConfig` for the sub-agent without code changes — this milestone is the first real test that `0009`'s declarative design actually achieved reusability, not just an abstraction that looks reusable) and `0011` (the sub-agent's verified result is what mints a `Fact`, closing the loop the old architecture used to force "the model claims X" into "a fact citing exact evidence").

## Design decisions

* **Nested delegation is disallowed.** The sub-agent's own action set excludes `delegate`, matching the old architecture's worker loop. This keeps recursion depth at exactly one level and the first version simple.
* **Read-only sub-agent action set.** The sub-agent gets `read_file` and `answer` only — no `write_file`, no `shell`, no `delegate`. A dedicated `register_delegate_actions(registry: ToolRegistry) -> None` (alongside the existing `register_actions`) wires exactly these two.
* **Own `Session`, same store.** The sub-run gets its own `Session` on the *same* SQLite connection as the parent (no new store/connection machinery needed) but a distinct `session_id` derived from the parent's, e.g. `f"{parent_session_id}/delegate/{n}"` where `n` is the count of delegate calls made so far in the parent run. This keeps delegation history inspectable (queryable by prefix) and bounded, without sharing the parent's event log directly.
* **Own `Bus`.** The sub-run publishes its streaming/tool events to a private `Bus()` instance, not the parent's — the sub-agent's turn-by-turn streaming is not user-facing TUI noise. Isolation here means the parent's bus contract is unchanged by this milestone: `delegate` shows up in the parent's `ToolCallStarted`/`ToolCallFinished` like any other action, nothing more granular from the sub-run leaks out.
* **Bounded step budget.** A module-level `MAX_DELEGATE_STEPS = 10` constant in `loop.py`, passed as the sub-run's `LoopConfig.max_steps`. If the sub-run hits the cap without producing a `final_answer` (e.g. `answer` never called, or only invalid `answer` calls), the `delegate` call is an error result — recoverable via the normal retry path, not fatal to the parent run.
* **Result shape.** `delegate`'s tool result string, on sub-run success, is the sub-answer's `content` alone (not a JSON blob) — this is what makes the parent's existing `Fact` minting (every successful `ToolCallRecorded`, per `0011`) pick it up with no ledger changes: the delegate's fact `content` is the sub-agent's answer, `source` is `"delegate"`. The sub-answer's own citations are resolved against the sub-session's own facts and are not re-exposed to the parent; they were already checked for existence inside the sub-run's own `tool_call_step`, so by the time `delegate` returns, the content is already a citation-checked, evidence-backed answer — that's what "evidence-cited result" means at the parent's boundary: the citing already happened one level down.
* **Context budget reused.** The sub-run gets its own `context_size` (reuse `runner.context_size` from the parent — one model, one context window, regardless of nesting depth) and calls `render_messages` exactly like the parent loop does; no special-casing needed since it is a real `LoopRunner`.

## [X] T001 `Delegate` action

### Description

In `src/pico/core/actions.py`:

* `Delegate(question: str)` — frozen dataclass, required `question: str`, built via `_require` like the other actions. No `execute` method — like `Answer`, `Delegate` doesn't produce its result by itself; running the sub-loop is `tool_call_step`'s job (T003) because it needs the runner's `llm`/`tools`/`bus`/`context_size`/`session` to construct the inner `LoopRunner`.
* Extend `Action` to `ReadFile | WriteFile | Shell | Answer | Delegate`.
* Add a `"delegate"` entry to `_TOOL_SPECS`: description makes clear this spawns a read-only sub-agent to answer a scoped question; `parameters` requires `question: string`.
* Add `"delegate"` to `ACTIONS`... note: `ACTIONS` does not exist yet in the current code (`register_actions` registers tools directly) — do not introduce it speculatively; only add what T004/T003 actually need. Register `delegate` as a tool the same way `answer` is registered in `register_actions`: present in `specs()` via `_TOOL_SPECS["delegate"]`, but with a no-op placeholder `execute` (mirroring `answer`'s `lambda _: ""` pattern), since real execution is special-cased in `tool_call_step`.

### Acceptance criteria

* `tests/core/test_actions.py` covers: `Delegate.from_arguments` success; `InvalidActionError` for a missing `question` field; `register_actions` still populates exactly `{"read_file", "write_file", "shell", "answer", "delegate"}`.
* Fully annotated, passes strict Pyright.

## [X] T002 Read-only action set for sub-agents

### Description

In `src/pico/core/actions.py`, add `register_delegate_actions(registry: ToolRegistry) -> None` that registers only `read_file` and `answer` (reusing the same `_action_tool(ReadFile, "read_file")` and the same `answer` placeholder tool already used by `register_actions` — do not duplicate the `ToolSpec`s, share `_TOOL_SPECS`). No `write_file`, `shell`, or `delegate` in this registry, so a sub-agent physically cannot call them (`ToolRegistry.execute` raises `UnknownToolError` for anything not registered, which is the existing recoverable-error path — no new enforcement mechanism needed beyond "don't register it").

### Acceptance criteria

* `tests/core/test_actions.py` covers: `register_delegate_actions` populates exactly `{"read_file", "answer"}` into a `ToolRegistry`'s `specs()`.
* Fully annotated, passes strict Pyright.

## [X] T003 Wire `delegate` into the loop: spawn a bounded inner `LoopRunner`

### Description

In `src/pico/core/loop.py`:

* Add `MAX_DELEGATE_STEPS = 10` alongside `MAX_INVALID_ACTION_ATTEMPTS`.
* Add a `LoopRunner` counter `delegate_calls: int = 0` (same transient-state pattern as `pending_tool_calls`/`invalid_action_attempts`), incremented each time `tool_call_step` handles a `delegate` call — used to derive the child `session_id` suffix.
* In `tool_call_step`, add a branch for `call.name == "delegate"` alongside the existing `"answer"` branch:
  1. Parse `Delegate.from_arguments(call.arguments)`; an `InvalidActionError` here follows the exact same `is_error=True` recording path as every other invalid action (reuse the existing `except InvalidActionError` shape — refactor the `if/elif` slightly if needed to avoid duplicating the error-recording block, but do not introduce a new abstraction beyond what's needed to share it).
  2. On success: increment `runner.delegate_calls`; build a child session via `Session(runner.session._conn, f"{runner.session_id}/delegate/{runner.delegate_calls}")` — this requires `Session` to expose its `session_id` (add a `session_id` property to `Session` in `src/pico/session/session.py` if it doesn't already expose one; check before adding) and requires `LoopRunner` to expose the *connection* the session uses (add whatever minimal accessor is needed — prefer a `Session` method like `Session.child(suffix: str) -> Session` over reaching into `Session`'s private `_conn` from `loop.py`, since `loop.py` should not touch another module's underscore-prefixed attributes).
  3. Append the question as a `UserMessageRecorded` to the child session.
  4. Build a read-only `ToolRegistry` via `register_delegate_actions`.
  5. Construct a child `LoopRunner(runner.llm, child_tools, Bus(), child_session, runner.context_size, LoopConfig(steps=(stream_step, tool_call_step), max_steps=MAX_DELEGATE_STEPS))` and call `.execute()`.
  6. If the child runner's `final_answer` is set after `execute()`, that string is the `delegate` call's successful result (`is_error=False`). If not (budget exhausted or the child never answered), the result is an error string naming the question and that the sub-agent did not produce an answer within its step budget (`is_error=True`).
* Publish `ToolCallStarted`/`ToolCallFinished` for the `delegate` call on the *parent* bus exactly as for any other action (started before spawning the child runner, finished after it returns) — the child's own bus events stay on the child's private `Bus` and are not forwarded.
* Record `ToolCallRecorded(name="delegate", arguments=call.arguments, result=<content-or-error>, is_error=...)` on the *parent* session, same as any other action — this is what lets `0011`'s existing `facts()` mint a `Fact` from a successful delegation with no ledger code changes.

### Acceptance criteria

* `tests/core/test_loop.py` covers: a `delegate` call whose sub-agent answers within budget records a successful `ToolCallRecorded` on the parent session with the sub-answer's content as `result`, and that content becomes a `Fact` via `pico.core.ledger.facts` on the parent session; a `delegate` call whose sub-agent never calls `answer` before `MAX_DELEGATE_STEPS` is exhausted records an error `ToolCallRecorded` and does not mint a fact; the sub-agent's tool registry cannot execute `write_file`/`shell`/`delegate` (assert via a scripted sub-agent LLM response requesting one of these and observing an `UnknownToolError`-derived error result recorded in the *child* session, ending that child run through the normal invalid-action path — not a crash); the child session's events are stored under a distinct `session_id` from the parent (queryable independently via a fresh `Session` against the same connection); the child run's own streaming events do not appear on the parent's `Bus`.
* Fully annotated, passes strict Pyright.

## [X] T004 Wire `delegate` into the running app

### Description

`src/pico/app.py`: no change should be needed if `register_actions` (T001) already includes `delegate` in the default action set built for the top-level `ToolRegistry` — confirm this is the case (the top-level loop is the "parent" that's allowed to delegate; it is never itself a sub-agent). If `register_actions` and `register_delegate_actions` need to share tool-building code, keep the sharing inside `actions.py` (e.g. both call the same private helpers per tool) rather than adding anything to `app.py`.

### Acceptance criteria

* `grep -rn "register_delegate_actions\|Delegate" src/pico/app.py` shows no direct references needed (confirms the split is fully internal to `actions.py`/`loop.py`), OR if a reference turns out to be necessary, it is minimal and justified in this task's notes.
* `uv run python -m pico` starts with `delegate` present among the top-level model's tools (manually confirm via a lightweight seam matching existing app tests, or note if no live LLM endpoint is reachable).

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` to re-export the new public surface (`Delegate`, `register_delegate_actions`, `MAX_DELEGATE_STEPS`).

### Acceptance criteria

* `make check` passes with no errors.
* `grep -rn "def _step\b" src` still returns nothing (no parallel hand-rolled dispatch introduced for delegation).
* A test exercises a full parent-delegates-to-child round trip end-to-end (T003's first scenario) using a real `:memory:`-backed connection shared by both sessions, not mocks of `Session`.

### Completion

Commit:
