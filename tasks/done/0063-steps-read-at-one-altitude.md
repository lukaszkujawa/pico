# Steps Read at One Altitude

Several step functions interleave three jobs in one body: policy (should this happen at all), execution (do the thing), and ceremony (bus publishes plus session appends that record what happened). `step_orchestration_step` (subruns.py) is the clearest case — depth/state guards, then plan lookup, then a retry-signature budget check, then a spawn, then fourteen lines of `ToolCallStarted`/`ToolCallRecorded`/`PlanStepCompleted`/`ToolCallFinished` plumbing, all at the same visual level. The reader cannot see the orchestration decision for the bookkeeping. The same shape recurs: `tool_call_step` (dispatch.py) mixes per-call dispatch, exception translation, the `Answered` lifecycle transition, and invalid-attempt policy in one 49-line loop body; `decision_step` (policy.py) inlines a synthetic answer-pane publication inside a `match` arm on a decision command. And the ceremony itself is duplicated — the `facts(session)[-1].id if not is_error else None` fact-id dance plus `ToolCallFinished` publish appears verbatim in both dispatch.py and subruns.py, two hand-rolled copies of "record a finished tool call".

The problem is not the return count — the early guards in these functions are the right idiom and stay. The problem is altitude: policy decisions and event plumbing share one body, so neither reads well. The fix is extraction into expressive single-job functions, so each step function reads as its decision sequence and the ceremony lives in one named place.

## Design decisions

* **Guard clauses stay; single-exit is a non-goal.** Early returns for "not applicable" are the house idiom (`conclude`, `spawn_delegate`, `restriction` already read this way). No function is contorted to a single return; extraction, not exit-counting, is the readability lever.
* **One tool-call ceremony helper.** The record-and-publish sequence (append `ToolCallRecorded`, derive the fact id, publish `ToolCallFinished`) is written once and used by both dispatch.py and subruns.py. The `AnswerSettled` branch stays with dispatch — the helper covers the shared shape, not every publish.
* **Each step function names its policy.** The retry-signature block in `step_orchestration_step` becomes a function whose name states the rule (a step gets `MAX_STEP_ATTEMPTS` tries against an unchanged plan). The invalid-attempt counting in `tool_call_step` likewise gets a name instead of living inline between dispatch and outcome selection.
* **A step function's body is its decision sequence.** After extraction, `step_orchestration_step` reads: guards → retry budget → run the step and record it. `tool_call_step` reads: for each call → execute → record → apply failure policy. The helpers hold the how; the step holds the what.
* **Behaviour-preserving.** No event is added, removed, or reordered; `MAX_STEP_ATTEMPTS`, `MAX_INVALID_ACTION_ATTEMPTS`, and every published payload are unchanged. Scope excludes `stream` and the TUI's `translate` — long `match` dispatchers over event types are a different, acceptable shape.

## [X] T001 Shared tool-call ceremony

### Description

Extract the append-record-then-publish-finished sequence (including the fact-id derivation) into one helper used by dispatch.py's `_record` and subruns.py's `step_orchestration_step`.

### Acceptance criteria

* `facts(session)[-1].id if not is_error else None` appears in exactly one place.
* Both call sites publish byte-identical events to before, shown by existing loop tests passing without payload changes.
* `make check` passes.

## [X] T002 Step orchestration reads as its decision sequence

### Description

Split `step_orchestration_step` so the retry-signature policy and the run-and-record ceremony are named helpers; the top level is guards, retry budget, then one call that runs the step and records the outcome.

### Acceptance criteria

* The top-level function contains no bus publishes and no direct `attempts` dict manipulation; those live in helpers whose names state their job.
* The retry rule is covered by a test that exercises the same-plan/changed-plan signature behaviour through the helper.
* `make check` passes.

## [X] T003 Tool-call step separates dispatch from policy

### Description

Extract the execute-one-call body of `tool_call_step` (context construction, dispatch, exception translation) into a helper returning the call's result, leaving the loop with recording, invalid-attempt policy, and outcome selection.

### Acceptance criteria

* `tool_call_step`'s loop body reads as execute → record → apply policy; the try/except ladder lives in the helper.
* The `Answered` lifecycle transition has one owner, stated where it happens, not buried mid-try.
* Loop tests pass with mechanical updates only.
* `make check` passes.

## [X] T004 Decision step delegates its ceremony

### Description

Move the synthetic answer-pane publication in `decision_step`'s `EndDegraded` arm into a named helper so the `match` reads as command → response, one line per arm.

### Acceptance criteria

* Each arm of the `match` in `decision_step` is a state change plus at most one helper call.
* The degraded-answer events are unchanged in content and order.
* `make check` passes.
