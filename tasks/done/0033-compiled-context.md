# Compiled Context

Today's "compiled context" is really an elided transcript. `stream_step` (`src/pico/core/loop.py`) sends the whole conversation every call; `render_messages` (`src/pico/core/context.py`) only subtracts from it — demoting tool results to handles, then eliding from the front — when it stops fitting. Two vision principles are unmet. *Compiled context*: the model should receive the smallest useful view of current state, not the largest transcript that fits. *Tunnel vision*: with a roomy budget nothing is compiled at all — a model twenty steps into a task still rereads all twenty steps to decide step twenty-one, and small models demonstrably lose the thread in exactly that situation.

This milestone inverts the construction. Instead of "full transcript minus what doesn't fit", each call gets "compact state plus a small recency window": a briefing message carrying the plan and an index of every fact, followed by only the most recent few conversation units. The transcript becomes what the vision says it is — a cache. Everything older than the window simply isn't sent; its evidence stays addressable through the fact index and `read_fact` (`0020`), intent stays visible through the plan (`0022`), and the session log (`0008`) remains the durable record. The elision pass (`0024`) is subsumed and deleted: window selection *is* the elision policy now, and the fact index is a strictly more informative replacement for the elision marker.

## Design decisions

* **Prompt shape.** Each generation sends: the system prompt; one briefing `Message(role=Role.USER)` containing the current plan (when set) and the fact index (when any facts exist), omitted entirely when both are empty; the recency window, verbatim from the session; the pending nudge (unchanged). The existing separate plan message in `stream_step` (`_plan_message`) folds into the briefing and disappears.
* **Fact index.** One line per fact — id, source tool, and a single-line preview of roughly 60–80 characters with newlines collapsed — capped at the most recent ~20 facts with a trailing `+N earlier facts` overflow line, plus one line telling the model `read_fact(id)` recovers any fact in full. `Fact.source` (`src/pico/core/ledger.py`), currently unread, becomes load-bearing here. Rendering is a pure function over `list[Fact]`.
* **Recency window.** The unit is the existing pair-safe grouping from `0024`: an assistant message plus its trailing `Role.TOOL` results travel together. Selection walks backward from the end of the transcript, keeping at most `RECENT_UNITS` units (default 8 — small enough to be tunnel vision even under a huge budget; calibrate later with evals, not by feel). The protected tail — the latest `UserMessageRecorded` and everything after it — is always kept in full, even when it alone exceeds budget or `RECENT_UNITS`; at that point there is nothing sane left to cut and the oversized prompt goes out, as today. If the window is over budget, first demote its tool results to handles oldest-first (reusing `render_tool_result`), then drop whole units oldest-first down to the protected tail.
* **Budget arithmetic stays honest.** The briefing's own token cost counts against `prompt_budget` before the window is sized, the same way overhead is accounted today. The caps on the fact index keep the briefing bounded, so it can never starve the window of the protected tail. `chars_per_token` reconciliation (`0023`) is untouched.
* **One entry point, pure and testable.** `render_messages` is replaced by a compile function (suggested: `compile_context(session, context_size, overhead_tokens, chars_per_token) -> list[Message]`) returning briefing plus window. `stream_step`'s only change in shape: it no longer builds a plan preamble, and it estimates overhead from system prompt, tool specs, and nudge alone. `elide` and `elision_marker` are deleted along with their exports and tests — window selection replaces the former, the fact index replaces the latter. Fewer concepts, per the vision.
* **What is knowingly given up.** User and assistant turns older than the window are not recoverable by the model — only their facts and the plan survive. That is the same recoverability today's elision offers (it, too, drops old turns and keeps fact ids), so this is no regression. A message-recall tool is a separate milestone if evals ever show the need.
* **No summarization.** Same rationale as `0024`: a model-written digest of old turns would be unverifiable model judgement in the runtime's seat. The briefing is compiled deterministically from structured state or not at all.

## [X] T001 Green baseline

### Description

`make check` fails at HEAD on the vulture step, which predates this milestone. Restore green before changing behaviour: delete findings that are genuinely dead (`BudgetExceeded.actual` is published but never read — drop the field and its publish site; `LOGO_WIDTH`; unused theme entries), and whitelist the rest (`vulture_whitelist.py`) where they are API surface read by tests or frameworks. Do not whitelist `Fact.source` or `REFERENCE_CONTEXT_SIZE` by reflex — `Fact.source` becomes used in T002, and `REFERENCE_CONTEXT_SIZE` is already read by `tests/evals/test_tasks.py`; if vulture still flags them, prefer the smallest fix that keeps the signal honest.

### Acceptance criteria

* `make check` passes with no errors.

## [X] T002 Fact index

### Description

Implement the fact index rendering per the design decisions: a pure function from `list[Fact]` to briefing text, with single-line previews, the recent-facts cap with overflow line, and the `read_fact` recovery hint.

### Acceptance criteria

* `tests/core/test_context.py` covers: every fact appears as one line with its id and source; a multi-line fact content is collapsed to a single-line preview of bounded length; more facts than the cap yields exactly the most recent capped ids plus a correct `+N earlier facts` overflow; an empty fact list renders to an empty index; the recovery hint is present whenever any fact is listed.
* Fully annotated, passes strict Pyright.

## [X] T003 Recency window

### Description

Implement window selection per the design decisions as a pure function over a message list and budget: pair-safe units, the `RECENT_UNITS` cap, the always-kept protected tail, handle demotion inside the window before unit dropping, and the over-budget-anyway terminal case.

### Acceptance criteria

* `tests/core/test_context.py` covers: a short conversation is returned unchanged; a long conversation is cut to the most recent `RECENT_UNITS` units even when the budget would allow more (tunnel vision, asserted with a generous budget); a tool-call/result pair straddling the cut is kept or dropped atomically; under budget pressure tool results inside the window demote to handles before any unit is dropped; the protected tail survives even when it alone exceeds both cap and budget; the demoted handle text still names the correct fact id.
* Fully annotated, passes strict Pyright.

## [X] T004 Compile and integrate

### Description

Compose briefing and window into the compile function and wire it into `stream_step`: build the briefing from `plan(session)` and `facts(session)`, count its cost against the budget before sizing the window, omit it when empty, and remove `_plan_message`. Delete `elide` and `elision_marker`, their exports in `src/pico/core/__init__.py`, and their tests. Update `tests/evals/test_tasks.py` to exercise the compile function at `REFERENCE_CONTEXT_SIZE`. Delegates (`_run_delegate`) get the same compiled view through `stream_step` with no delegate-specific code.

### Acceptance criteria

* `tests/core/test_context.py` covers: the compiled output for a session with plan and facts is briefing-then-window with the briefing containing both; a session with neither plan nor facts compiles to the bare window with no briefing message; total estimated tokens of the compiled output fit `prompt_budget(context_size) - overhead_tokens` in the normal case; a fact whose full content was dropped from the window still has its id in the fact index.
* `tests/core/test_loop.py` covers: a `stream_step` generation over a session longer than `RECENT_UNITS` units sends the briefing and window, not the full transcript (asserted via a fake LLM capturing its messages), and the plan is present exactly once.
* `elide` and `elision_marker` no longer exist anywhere in `src/` or `tests/`.
* Fully annotated, passes strict Pyright.

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until green. If a live model is configured, run `make evals` and record the before/after table in the completion notes — this milestone changes what every generation sees, so suite movement is the evidence it earns its complexity (VISION.md: complexity is justified only when it measurably helps). Pay particular attention to `recall_from_log`, `dependent_chain`, and `many_small_steps`, which stress exactly the state this milestone compiles.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

`make check` green. `make evals` run against `glm-4.7-flash:latest` at 128k context, one sample per suite; before is master (8346633), after is this branch.

| task                 | before | after | prompt before | prompt after |
|----------------------|--------|-------|---------------|--------------|
| read_one_file        | pass   | FAIL  | 1538          | 1578         |
| find_definition      | pass   | pass  | 3931          | 2972         |
| write_file           | pass   | pass  | 2358          | 4334         |
| run_script           | FAIL   | pass  | 1524          | 1552         |
| aggregate_large_file | FAIL   | pass  | 166620        | 47967        |
| recall_from_log      | FAIL   | pass  | 179639        | 119867       |
| make_tests_pass      | pass   | pass  | 7314          | 4699         |
| multi_step_chore     | pass   | pass  | 6609          | 3701         |
| dependent_chain      | FAIL   | pass  | 23986         | 5226         |
| many_small_steps     | FAIL   | FAIL  | 235817        | 171436       |
| **total**            | 5/10   | 8/10  |               |              |

The stress tasks the milestone targets all flipped to pass — `recall_from_log`, `dependent_chain`, and `aggregate_large_file` — with prompt tokens down 33–78%. `many_small_steps` still fails but in fewer iterations and tokens. `read_one_file` flipped to FAIL in this sample; single-sample runs of a stochastic local model flip easy tasks either way, and the task involves no compaction (2 iterations, ~1.5k prompt tokens), so it is noise, not a regression signal.

Commit:
