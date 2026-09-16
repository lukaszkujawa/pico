# Conversation Compaction

The context budget has exactly one lever: `render_messages` (`src/pico/core/context.py`) shrinks non-error tool results to handles, oldest first, until the total fits `prompt_budget`. When that isn't enough — long conversations, verbose assistant turns, many small tool calls, big user messages, none of which the handle pass touches — the loop exits having done its best and the over-budget message list is sent anyway. On a frontier model with a huge window this is theoretical; on the small models Pico exists for, it is an eventual certainty on any long session, and what happens next (silent server-side truncation, degraded output, a hard error) is up to Ollama, not the runtime. "Compiled context" must not mean "compiled until it stops fitting."

This milestone adds the second tier: when handles alone can't fit the budget, elide whole turns from the front of the conversation, replacing them with a single marker that says what was dropped and how to get it back. It depends on `0023` — elision decisions are only as good as the budget arithmetic they consult, so the honest accounting lands first. Everything remains in the log; `read_fact` (`0020`) keeps elided evidence addressable; the plan injection (`0022`) keeps intent visible; recency keeps the working set. This is the vision's recoverability contract applied one level up: hiding is fine, losing is not.

## Design decisions

* **Elision is a second pass inside `render_messages`, oldest-first, pair-safe.** After the existing handle pass, while still over budget, drop messages from the front. An assistant tool-call message and its `Role.TOOL` result are one unit — drop both or neither (a dangling half is malformed input for the chat API). Never drop into the protected tail: the most recent `UserMessageRecorded`'s message and everything after it always survives, even if that alone exceeds budget — at that point there is nothing sane left to cut and the oversized prompt goes out as it does today.
* **One marker, leading, informative.** When anything was elided, the returned list starts with one `Message(role=Role.USER)`: how many messages were elided and the fact ids that went with them — `[42 earlier messages elided to fit the context budget — their evidence remains available: read_fact(3), read_fact(17), ...]` — capped at the most recent dozen or so ids with a `+N more` suffix rather than growing without bound. The marker is render-output only, never persisted, and its own (small) cost is counted against the budget.
* **Pure and testable.** The elision logic lives in its own pure function (e.g. `elide(messages, budget) -> list[Message]` — name and exact signature your call) so tests exercise it directly on constructed message lists without a session, mirroring how the handle pass is already tested. `render_messages` composes the two passes.
* **No summarization.** A model-written summary of elided turns would put a model in charge of deciding what mattered — the opposite of this runtime's bet, and unverifiable. Deterministic elision plus recoverable facts is the whole design. If evals later show summaries earn their complexity, that is a separate milestone with numbers attached.

## [X] T001 Pair-safe elision pass

### Description

Implement the elision function and compose it into `render_messages` after the handle pass, per the design decisions (protected tail, tool-call pairing, over-budget-anyway terminal case).

### Acceptance criteria

* `tests/core/test_context.py` covers: a conversation that fits after handles alone is returned unchanged by the second pass; one that doesn't gets oldest messages dropped until it fits; a tool-call/result pair straddling the cut line is dropped or kept atomically; the latest user message and everything after it survive even when that tail alone exceeds budget; an empty conversation and a single-turn conversation pass through untouched.
* Fully annotated, passes strict Pyright.

## [X] T002 Elision marker

### Description

Build the marker message per the design decisions: elided-message count, the trailing window of elided fact ids with `+N more` overflow, budget-counted, prepended only when elision occurred.

### Acceptance criteria

* `tests/core/test_context.py` covers: the marker names the exact count and the correct ids (asserted against a session with interleaved error calls, whose ids must not appear); the id list caps with the `+N more` suffix; no marker appears when nothing was elided; the returned total including the marker fits the budget in the normal case.
* Fully annotated, passes strict Pyright.

## [X] T003 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Extend the `0021` suite with one task whose conversation (not just one tool result) outgrows the reference budget — e.g. a many-small-steps chore — and whose `check` verifies the end state, confirming the run survives compaction. If a live model is configured, run `make evals` and note movement in the commit message.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
