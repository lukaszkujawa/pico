# Ledger

Roadmap milestone 4 of 7 (see `0008-session-store.md` for the full arc). Broken into tasks below.

Adds the epistemic-discipline layer from the old architecture's `ledger.py`, scoped to what's useful once real actions (`0010`) exist: typed `Goal` and `Fact` entities (start with these two; `Constraint`/`Question`/`Hypothesis` from the old architecture are candidates to add later only if a concrete need appears — do not port all five speculatively) derived from the session event log (`0008`), queryable via SQL against the same SQLite store rather than held as separate mutable state.

## Design decisions (scoped for this milestone, before `0013` delegation exists)

* **No new event types.** `Goal` and `Fact` are *derived* by replaying `Session.events()`, exactly like `Session.messages()` already derives message history — never appended as their own event kind, never held as mutable state that could drift from the log.
* **`Goal`**: the most recent `UserMessageRecorded` in the log is the active goal. Pico has no sub-goal/planning concept yet, so "latest user message" is the simplest correct reading — one `Goal` per session state, not a stack or tree.
* **`Fact` minting gate, decided explicitly per the brief's request**: the old architecture's strict rule (facts can only be minted by a delegate sub-agent) depends on `0013`, which doesn't exist yet. For this milestone, the gate is looser and documented as an explicit interim rule: *every successful (`is_error=False`) `ToolCallRecorded` mints exactly one `Fact`*, keyed by its position in the replayed log. This is the "perhaps any successful action result can become a fact" option the brief offers. When `0013` lands, tightening this gate (e.g. restricting minting to `delegate` results only) is that milestone's job, not this one's — do not build a strictness toggle or config flag for it now, that would be speculative.
* **The core discipline**: `answer` must only be able to cite known facts. Concretely, `Answer` gains a required `citations` field: a list of fact indices. Validating an `answer` call now needs the session's current facts (not just the raw arguments), so citation-checking happens where the session is available (`tool_call_step`), not inside `Answer.from_arguments` (which stays a pure function of arguments alone, consistent with every other action). An `answer` citing an unknown fact index is an invalid action — recoverable via the existing retry path (`0010`), not fatal.
* **No planning/constraint/hypothesis machinery.** Out of scope for this milestone.

## [X] T001 `Fact` and `Goal`: derived ledger types

### Description

New module `src/pico/core/ledger.py`:

* `Fact(index: int, content: str, source: str)` — frozen dataclass. `index` is the fact's position among *facts only* (0-based, in log order) — this is the number a model uses in `answer`'s `citations`, so it must be small and stable, not the raw event `seq`. `content` is the tool result string. `source` is the action name that produced it (`call.name` from the originating `ToolCallRecorded`).
* `Goal(content: str)` — frozen dataclass wrapping the latest user message.
* `facts(session: Session) -> list[Fact]` — replays `session.events()`, projects every `ToolCallRecorded` with `is_error=False` into a `Fact`, assigning `index` sequentially in the order encountered.
* `goal(session: Session) -> Goal | None` — replays `session.events()`, returns a `Goal` built from the last `UserMessageRecorded` seen, or `None` if the session has no user messages yet.

Both functions are pure reads over `session.events()` — no caching, no new store table, matching `0008`'s "derive by replaying" principle exactly.

### Acceptance criteria

* `tests/core/test_ledger.py` (new) covers: `facts` returns `[]` for an empty session; `facts` skips `is_error=True` tool calls; `facts` assigns sequential 0-based indices across a mix of user/assistant/tool events, ignoring non-tool events; `facts` ignores `UserMessageRecorded`/`AssistantMessageRecorded`; `goal` returns `None` for a session with no user messages; `goal` returns the latest `UserMessageRecorded` content when multiple exist.
* Fully annotated, passes strict Pyright.

## [X] T002 `Answer` requires citations

### Description

In `src/pico/core/actions.py`:

* Add `citations: tuple[int, ...]` to `Answer`, required. `from_arguments` must validate the raw argument is a list/tuple of ints — `_require`'s plain `isinstance` check isn't enough (JSON arrays deserialize as `list[object]`, and element types need checking too), so add a small dedicated helper (e.g. `_require_int_list`) alongside `_require` rather than overloading `_require` itself with per-element logic. Missing `citations`, a non-list value, or a list containing a non-int element all raise `InvalidActionError` naming the problem.
* Update `_TOOL_SPECS["answer"]`'s JSON schema to include `citations` (`{"type": "array", "items": {"type": "integer"}}`) in `properties` and `required`.

Citation *existence* (do the indices refer to real facts) is not checked here — `Answer.from_arguments` only validates shape, since it has no access to the session's facts. Existence is `tool_call_step`'s job (T003), where the session is available.

### Acceptance criteria

* `tests/core/test_actions.py` covers: `Answer.from_arguments` success with a valid `citations` list; `InvalidActionError` for missing `citations`; `InvalidActionError` for a `citations` value that isn't a list; `InvalidActionError` for a list containing a non-int element (e.g. a string).
* Fully annotated, passes strict Pyright.

## [X] T003 Gate `answer` on known facts in the loop

### Description

In `src/pico/core/loop.py`, `tool_call_step`'s existing `answer` special-case: after `Answer.from_arguments(call.arguments)` succeeds, compute `known = {f.index for f in ledger.facts(runner.session)}` and check every entry in `answer.citations` is in `known`. If any citation is unknown, treat it exactly like an `InvalidActionError` from validation — same recoverable path: record a `ToolCallRecorded(is_error=True)` with a descriptive message (name the bad index), return `"continue"`, increment `runner.invalid_action_attempts` (already happens generically via the existing `is_error` branch — confirm this, don't duplicate the increment). Only when `citations` both validate structurally (T002) and resolve to known facts does the run end with `"done"` and `runner.final_answer` set.

An `answer` with an empty `citations` tuple is valid shape-wise (T002 only requires the field be present and correctly typed, not non-empty) and trivially passes the "every citation is known" check since there's nothing to check — this is deliberate: not every answer necessarily needs to cite evidence (e.g. answering a question that requires no tool use), and forcing non-empty citations is a stricter rule than this milestone's scope calls for.

### Acceptance criteria

* `tests/core/test_loop.py` covers: an `answer` citing valid known fact indices ends the run with `"done"` and the expected `final_answer`; an `answer` citing an unknown index returns `"continue"` with an error recorded (not `"done"`) and does not set `final_answer`; an `answer` with empty `citations` still succeeds and ends the run.
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` to re-export the new public surface (`Fact`, `Goal`, `facts`, `goal`).

### Acceptance criteria

* `make check` passes with no errors.
* `grep -rn "citations" src/pico/core/actions.py src/pico/core/loop.py` shows the field wired through both validation and the loop's gate.

### Completion

Commit:
