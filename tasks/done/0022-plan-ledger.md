# Plan Ledger

`VISION.md` lists "durable facts, goals, plans, results" as software-managed state. Facts have the ledger; goals are (barely) the last user message; plans do not exist. The runtime keeps no representation of what the model intends to do, so on a long task a small model must re-derive its own plan from conversation scrollback every turn — precisely the long-horizon reasoning the vision says the runtime should carry. When the budget squeezes old turns into handles (and `0024` will elide them entirely), an in-context plan written as prose in some early assistant turn is the first thing to disappear.

This milestone gives plans the same treatment facts got: durable events in the session log, a pure replay function in the ledger, deterministic runtime bookkeeping, and a guaranteed spot in every rendered context. The model decides *what* the plan is; the runtime remembers it, tracks completion, and puts it in front of the model every single turn — "the runtime should remember, organise ... the model should reason and make decisions."

## Design decisions

* **Two events, replay for state.** `PlanSet(steps: tuple[str, ...])` replaces the whole plan; `PlanStepCompleted(index: int)` marks one step done. Both join `SessionEvent` (`src/pico/session/events.py`) and the `_EVENT_KINDS` registry (`src/pico/session/session.py`). Current plan state is derived fresh by `plan(session) -> Plan | None` in `src/pico/core/ledger.py` (`Plan` holds `steps: tuple[PlanStep, ...]`, `PlanStep` holds `text: str` and `done: bool`), same no-hidden-state style as `facts()`/`goal()`. A later `PlanSet` resets completion.
* **Two tools, validated mechanically.** `set_plan(steps: list of strings)` and `complete_step(index: integer)` in `src/pico/core/actions.py`. Validation is the runtime's job: `set_plan` rejects an empty list or non-string elements (`InvalidActionError`, reusing `_require`-style checks); `complete_step` raises `ToolError` (`0020`) for an out-of-range index or an already-done step. Tool results are small confirmations rendering the updated checklist, so the model gets immediate feedback in-band. Both close over the session the way `fact_recall_tool` does; registered in `register_actions` but **not** `register_delegate_actions` — a delegate's horizon is one question.
* **`messages()` ignores plan events.** They are working state, not conversation. `facts()` is untouched — a plan is not evidence and is not citable.
* **Injection at render time, never persisted as conversation.** `stream_step` (`src/pico/core/loop.py`) already appends a transient nudge message; the plan gets the same treatment at the other end: when `plan(session)` is non-`None`, insert one `Message(role=Role.USER)` immediately after the conversation's start rendering the checklist (`[x] step one` / `[ ] step two`, plus a one-line instruction to keep it current via `set_plan`/`complete_step`). It is rebuilt from the ledger every call, so it is always correct, always present regardless of what the budget did to the rest of the context, and appears exactly once.
* **No TUI work.** `set_plan`/`complete_step` calls already surface through the existing `ToolCallPane` rendering like any other tool. A dedicated plan widget is scope for a later milestone if the eval numbers say plans earn it.

## [X] T001 Plan events and ledger replay

### Description

Add the two events, register their kinds, and implement `PlanStep`/`Plan`/`plan()` in `src/pico/core/ledger.py` per the design decisions. Ensure `Session.messages()` skips the new kinds.

### Acceptance criteria

* `tests/session/test_events.py` / `tests/session/test_session.py` cover: both events round-trip through `append`/`events()`; `messages()` output is unchanged by their presence.
* `tests/core/test_ledger.py` covers: no plan events yields `None`; `PlanSet` then two `PlanStepCompleted` yields the right done flags; a second `PlanSet` discards prior completion; completion events for indices from a superseded plan don't corrupt the current one (define the rule: a `PlanStepCompleted` applies to the plan most recently set before it).
* Fully annotated, passes strict Pyright.

## [X] T002 `set_plan` and `complete_step` tools

### Description

Add the specs and session-closing tool factories to `src/pico/core/actions.py`, with the validation and confirmation-rendering behavior from the design decisions. Register in `register_actions` only; update `src/pico/app.py` call site if the signature shifts.

### Acceptance criteria

* `tests/core/test_actions.py` covers: `set_plan` with valid steps appends `PlanSet` and returns the rendered checklist; empty/malformed steps raise `InvalidActionError` and append nothing; `complete_step` on a valid index appends the event and the confirmation shows that box checked; out-of-range and already-done indices raise `ToolError` and append nothing; the delegate registry does not contain either tool.
* Fully annotated, passes strict Pyright.

## [X] T003 Render-time plan injection

### Description

Wire the transient plan message into `stream_step` per the design decisions. The plan message does not count against the trimming logic in `render_messages` (it is injected after rendering, like the nudge) and must never be written to the session.

### Acceptance criteria

* `tests/core/test_loop.py` covers, with a recording LLM client: when a plan exists, every subsequent LLM call's message list contains exactly one plan message with current checkbox state (set plan → complete a step → assert the next call shows the updated state); with no plan, message lists are byte-identical to pre-milestone shapes; `session.events()` contains no plan-rendering text, only the plan events themselves.
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` and `src/pico/session/__init__.py` re-exports. Add one eval task to the `0021` suite whose horizon is long enough that plan upkeep plausibly matters (extend task 8's family: many dependent steps, check verifies end state). If a live model is configured, run `make evals` and note pass/fail movement in the commit message.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
