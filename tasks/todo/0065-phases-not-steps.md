# Phases, Not a Sack of Steps

`DEFAULT_LOOP_STEPS` lists eight entries as if they were peers, but they are three different kinds of thing. Three are real phases of an iteration — `step_orchestration_step` (maybe run a child), `generation_step` (talk to the LLM), `tool_call_step` (act on the world). Three are policy rules with an identical shape — `stuckness_step`, `budget_step`, `decision_step` all look at the situation, maybe emit a nudge, maybe transition the run state. Two are not steps at all: `snapshot_step` is input preparation (it builds the `IterationView` three later steps silently depend on), and `lifecycle_step` is a single state transition. Flattening these into one reorderable tuple hides the contracts: steps communicate by ambient mutation of runner fields (`view`, `pending_tool_calls`, `active_restriction`), `observe()` secretly *clears* `narration_pressure` while building a supposedly read-only snapshot, and the budget concern speaks from three places (`execute()`'s hard stop, `budget_step`'s nudge, `pressure()` feeding the decision machine). Reordering the tuple breaks behaviour silently; reading it tells you nothing about which entries are load-bearing sequence and which are interchangeable rules.

## Design decisions

* **The default pipeline lists phases of the same kind.** The tuple shrinks to the real sequence: policy → orchestration → generation → dispatch. `snapshot_step` dissolves into the top of the policy phase (the view is built where it is consumed). The `Step` seam itself survives unchanged — `LoopRunner` still runs a tuple of `Step` callables, tests keep scripting with custom tuples, children inherit phases as they inherit steps today.
* **The `WindingDown → LastWords` transition keeps its exact position and leaves the tuple.** It must fire after orchestration and before generation (a step failure's wind-down becomes last words in the same iteration — 0061 established this deliberately). It becomes part of the fixed skeleton around the phases, not a listed entry a config could drop or misplace.
* **Policy rules share one value-returning contract.** Stuckness, budget, and decision become rules of one shape: take the view (and session where needed), return a verdict — nudges to emit, an optional transition, an optional command. The policy phase applies verdicts in one place; no rule mutates the runner. The existing `advance()` state machine slots in as-is; only its caller changes shape.
* **`observe()` becomes pure.** The `narration_pressure` consumption moves out of snapshot-building to the code that owns the flag's lifecycle. Building the view twice must yield the same view.
* **Budget speaks with one voice.** The hard stop stays in `execute()`. The 80% nudge and the pressure signal into the decision machine become one budget rule, so the two soft paths cannot disagree or double-fire in an iteration. Nudge texts are unchanged; only the emitting site consolidates.
* **Less code, verified.** This milestone deletes wiring (`snapshot_step`, `budget_step`, `lifecycle_step`, their exports and list entries); the rule contract may add a small type. Net line count of `src/pico/core/loop/` must not grow. No new abstraction is admitted unless it deletes more than it adds.
* **Behaviour-preserving.** Thresholds, nudge texts, decision-table values, event order, and the same-iteration wind-down semantics are unchanged. Scripted-LLM tests pass with mechanical updates only.

## [ ] T001 Policy rules behind one contract

### Description

Recast stuckness, budget, and decision as value-returning rules of one shape; the policy phase builds the view, runs the rules, and applies their verdicts in one place. Delete `budget_step`; make `observe()` side-effect free.

### Acceptance criteria

* The three rules share one signature and return values; none of them mutates `LoopRunner`.
* `observe()` (or its successor) has no side effects — a test builds the view twice and gets equal views.
* The budget nudge and budget pressure are produced by exactly one rule; `budget_step` no longer exists.
* `make check` passes.

## [ ] T002 The pipeline lists phases

### Description

Shrink `DEFAULT_LOOP_STEPS` to policy → orchestration → generation → dispatch, dissolve `snapshot_step`, and move the `WindingDown → LastWords` transition into the fixed skeleton between orchestration and generation.

### Acceptance criteria

* The default tuple has four entries, each a phase; `snapshot_step` and `lifecycle_step` are deleted.
* A wind-down set during orchestration still produces last words in the same iteration, shown by an existing or added test.
* Runner tests still script custom step tuples without change to the `Step` contract.
* Net line count of `src/pico/core/loop/` is not higher than before the milestone.
* `make check` passes.

## [ ] T003 The map matches the territory

### Description

Update `docs/core.md` — the iteration-pipeline diagram and any prose naming the old steps — to the phases-and-rules shape.

### Acceptance criteria

* No reference to `snapshot_step`, `budget_step`, or `lifecycle_step` remains in `docs/core.md`.
* The pipeline diagram shows the four phases with the skeleton-owned transitions, and the policy phase names its three rules.
* `make check` passes.
