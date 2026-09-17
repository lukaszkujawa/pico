# Loop Decomposition

`core/loop.py` has grown to 900 lines and is no longer pleasant to read. The step pipeline — `Step = Callable[[LoopRunner], StepOutcome]` run in order — is the right skeleton, but three things bury it. First, the steps communicate through fifteen-odd mutable attributes on `LoopRunner` (`pending_nudge`, `dying_of`, `crossroads`, `demanded`, `demanded_at`, `actionless_generations`, `step_attempts`, `chars_per_token`, …), so no step can be read in isolation: understanding one requires knowing which flags every other step reads or clobbers. Second, `stream_step` is three jobs in one 115-line function: prompt assembly, the streaming event pump, and post-generation policy. Third, policy is smeared — `decision_step` decides to restrict the vocabulary via the `crossroads` flag, `_restriction` re-derives the restriction two steps later inside the streaming path, and `_dispatch` enforces the same flags a third time — while 200 lines of `RUNNER_ACTIONS` tool schemas sit as data in the middle of the logic.

This milestone breaks the loop into a `core/loop/` package where each module owns one concern, holds its own state, and talks to the rest through one narrow, consistent interface. The measure of success is readability: each module should be understandable on its own, top to bottom, with no comments needed — names, types, and structure carry the explanation. Behaviour does not change except where a task names the change explicitly.

## Design decisions

* **The step pipeline stays.** `LoopRunner.execute` iterating `config.steps` is already the clearest possible engine; the work is relocating and disentangling what the steps do, not redesigning how they run.
* **Target layout.** `core/loop/` with `runner.py` (engine: `LoopRunner`, `LoopConfig`, `Step`, `StepOutcome`), `signals.py` (the inter-step contract), `policy.py` (stuckness, budget, decision steps), `prompt.py` (vocabulary and prompt assembly), `generate.py` (the streaming pump), `dispatch.py` (tool call execution), `builtins.py` (the runner-bound actions: shell, answer, search_facts, delegate), `subruns.py` (child runs, delegate execution, step orchestration, handoff composition). Every module lands well under 200 lines.
* **Four channels, one owner each.** After decomposition the runner carries exactly four cross-step channels: pending signals (policy writes, prompt assembly reads), the ending state (`dying_of` / `last_words`), the generation handoff (`pending_tool_calls` plus pane ids, generate writes, dispatch reads), and `final_answer`. Every other counter becomes private state of the single module that uses it, held in a small frozen-free dataclass composed into the runner (`DecisionState`, `DispatchState`, …). A reader of `policy.py` needs to know `Session` in and `Signal` out, nothing else.
* **One policy vocabulary.** Policy steps emit values from a closed union — `Nudge(text)`, `Restrict(allowed, nudge)`, `Die(cause)` — instead of poking flags. Restriction is computed once by policy and enforced once at dispatch; the duplicated checks in `_restriction` and `_dispatch` collapse into it.
* **Nudge semantics stay last-writer-wins, made explicit.** Today a later step silently overwrites `pending_nudge`; the signal channel keeps that outcome (the most urgent signal is emitted last in pipeline order) but makes the precedence visible in one place rather than an accident of assignment order.
* **`restricted_vocabulary` hardcodes `depth=0`** when rebuilding specs, which can offer `delegate` to a depth-limited node during a crossroads. Treat it as a latent bug: fix it where the code moves and pin it with a test.
* **The public surface is preserved through `loop/__init__.py`.** `app.py`, `headless.py`, and the existing tests import `DEFAULT_LOOP_CONFIG`, `LoopRunner`, and a long list of constants; re-exports keep every consumer working until T006 moves the tests to the new module paths.
* **`actions.py` stays put.** Unifying it with `builtins.py` into a single action package is real but separate work; this milestone only gives the runner-bound actions a consistent shape so that a later merge is mechanical.

## [X] T001 Mechanical package split

### Description

Convert `core/loop.py` into the `core/loop/` package with the modules named in the design decisions. Code moves verbatim — same functions, same names, same behaviour — placed by concern: engine to `runner.py`, the three policy steps and their helpers plus nudge text to `policy.py`, `stream_step` and its helpers to `generate.py`, `tool_call_step`, `_dispatch`, `_failed` to `dispatch.py`, `RUNNER_ACTIONS` with its executors, `AnswerOutcome`, answer verification, and `vocabulary` to `builtins.py`, child runs, delegate execution, step orchestration, and handoff composition to `subruns.py`. `__init__.py` re-exports everything `loop.py` exported today so no import outside `core/loop/` changes.

### Acceptance criteria

* `core/loop.py` is gone; each new module reads as one concern and is under ~250 lines.
* `app.py`, `headless.py`, and all tests are untouched and pass unchanged.
* No function body changed beyond import adjustments.
* `make check` passes.

## [X] T002 Per-concern state ownership

### Description

Slim `LoopRunner` down to the engine plus the four channels. Move each remaining counter into a dataclass owned by the module that uses it — decision tracking (`demanded`, `demanded_at`, `crossroads`, `crossroads_generations`, `last_narration`) into a `DecisionState` in `policy.py`, generation accounting (`actionless_generations`, `chars_per_token`, `tool_call_pane_ids`) into `generate.py`, dispatch accounting (`invalid_action_attempts`) into `dispatch.py`, orchestration retries (`step_attempts`) into `subruns.py` — composed into the runner as one attribute per module. Reading any module must no longer require knowing another module's internals.

### Acceptance criteria

* `LoopRunner` carries only constructor dependencies, the four channels, run bookkeeping (`iterations`, `error`, id source), and one state object per module.
* No module reads or writes another module's state object.
* Behaviour is unchanged; existing tests pass with at most renamed attribute access.
* `make check` passes.

## [X] T003 Signals as the policy interface

### Description

Introduce `signals.py` defining `Nudge`, `Restrict`, and `Die` as a closed union, and a single pending-signal slot on the runner with last-writer-wins precedence stated in its type and its one accessor. Rewrite `stuckness_step`, `budget_step`, and `decision_step` to emit signals instead of assigning `pending_nudge` or flag combinations. Prompt assembly consumes the signal to pick the vocabulary and the injected user message; dispatch consumes the same restriction to reject disallowed calls. Delete `_restriction` and the flag re-derivation in `_dispatch`. Fix `restricted_vocabulary` to respect the node's real depth.

### Acceptance criteria

* Every policy step has the shape: inspect session and own state, emit at most one `Signal`, return an outcome.
* Restriction is computed in exactly one place and enforced in exactly one place; grep finds no `crossroads` or `last_words` check outside policy and that enforcement point.
* A depth-limited node at a crossroads is never offered `delegate`; a regression test pins it.
* Nudge precedence is covered by a test asserting which signal wins when several steps emit in one iteration.
* All existing loop behaviours — wind-down, crossroads, degraded ending, last words — are preserved by the existing tests.
* `make check` passes.

## [X] T004 Split the generation step

### Description

Break `stream_step` along its natural seams into three functions with explicit data flow: `assemble(runner) -> Prompt` in `prompt.py` builds messages, specs, and token estimates from the compiled context and the pending signal; `stream(runner, prompt) -> Generation | None` in `generate.py` is the pure pump translating LLM deltas into bus events and returning `Generation(text, thinking, tool_calls)` or `None` on cancellation; `record(runner, generation) -> StepOutcome` appends to the session and applies the actionless-generation and narration policy. The composed `generation_step` replaces `stream_step` in the pipeline.

### Acceptance criteria

* Each of the three functions is readable on its own and no longer than ~60 lines; the pump contains no prompt assembly and no session writes.
* `Prompt` and `Generation` are small frozen dataclasses; the chars-per-token reconciliation lives with assembly, not the pump.
* Cancellation mid-stream, token accounting, and the budget-exceeded event behave exactly as before under the existing tests.
* `make check` passes.

## [X] T005 Consistent builtin action shape

### Description

Give the four runner-bound actions one uniform shape in `builtins.py`: each is a `RunnerAction` pairing a `ToolSpec` with an `execute` taking the same narrow context — the capabilities actions actually use (session, llm, pane streaming, child spawning) — and returning the same result type. Move each action's spec next to its executor so a reader sees one action completely before the next begins, instead of a 90-line schema table after 100 lines of executors. `vocabulary` becomes a pure function of the registry, depth, and active restriction.

### Acceptance criteria

* Each builtin action is one contiguous, self-contained block: spec then executor, uniform signature, uniform result type.
* `vocabulary` has one definition covering both the free and restricted cases.
* No action reaches into runner state beyond the narrow context it is handed.
* `make check` passes.

## [X] T006 Tests follow the modules

### Description

Split the 4,100-line `tests/core/test_loop.py` along the new module lines — `test_runner.py`, `test_policy.py`, `test_generate.py`, `test_dispatch.py`, `test_builtins.py`, `test_subruns.py` — moving each test next to the concern it exercises and importing from the concrete module rather than the package re-export. Shared fixtures and fakes go to a `tests/core/loop_fixtures.py` (or conftest) rather than being duplicated. Drop `__init__.py` re-exports that no longer have any consumer after the move.

### Acceptance criteria

* No test file under `tests/core/` exceeds ~1,000 lines; every moved test still passes.
* Test count is unchanged or higher; no test was deleted to make the split easier.
* `loop/__init__.py` exports only what `app.py`, `headless.py`, or tests still import.
* `make check` passes.

## [X] T007 Final soundness sweep

### Description

Run `make check` over the finished milestone and fix anything it surfaces — lint, format, strict typing, dead code, `tach` boundaries, tests, build. Delete any code the decomposition orphaned (`vulture` will name it), and make a final read-through pass over each `core/loop/` module purely for readability: names that say what things are, no leftover indirection, no comments.

### Acceptance criteria

* `make check` passes clean from a fresh run with no warnings ignored.
* No orphaned symbols remain from the old `loop.py` layout.
* Every module in `core/loop/` reads top to bottom as a single concern.
