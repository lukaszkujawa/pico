# One Iteration Snapshot

Pressure is derived three times per generation, in three files, from the same inputs — and they have already drifted. `decision_step` folds it into `Observations.pressure` (policy.py); `generation_step` rebuilds it for the `pressured` flag on `GenerationCompleted` (generate.py); `budget_remaining` is recomputed in `budget_step`, `decision_step`, and `generation_step`. Each was added by a later patch that reached for the value at a new call site instead of consolidating the source, so the same `transcript_fullness(session.messages(), context_size, chars_per_token)` and `budget_remaining(iterations, max_steps, depth, running)` incantations are copied across the loop and must be edited in lockstep. `advance` already proved the fix — a frozen snapshot in, a command out — but `Observations` is built inside `decision_step` and seen by nobody else. This milestone builds the snapshot once per iteration and makes every step read it.

## Design decisions

* **One `IterationView`**, frozen, built once at the top of the iteration from the runner: `iterations`, `remaining` (budget headroom), `fullness`, `undecided`, `narration`, and the derived `pressure: str | None` and `pressed: bool`. It subsumes `Observations` — `advance` takes the view (or a projection of it), not a second struct.
* **Built at the step edge, in one place.** A new `snapshot_step` (or a field the runner fills at iteration start) computes the view and stores it on the runner; `decision_step`, `budget_step`, and `generation_step` read `runner.view` instead of recomputing. No helper below the step layer takes the runner — `budget_remaining`, `transcript_fullness`, `pressure`, `undecided` stay pure and are called once, by the snapshot builder.
* **`GenerationCompleted.pressure` becomes `runner.view.pressed`.** The entire recompute in `generation_step` (fullness + `budget_remaining` + `pressure`, purely to colour a meter) collapses to one field read.
* **`budget_remaining` is computed once.** `budget_step` and the wind-down transition read the view's `remaining`; the value cannot disagree between the nudge and the decision that follows it.
* **Behaviour-preserving.** Thresholds (`BUDGET_WIND_DOWN_FRACTION`, `CONTEXT_PRESSURE_FRACTION`, `MAX_CROSSROADS`), nudge wording, and the `NARRATION_PRESSURE` special case are unchanged. `narration_pressure` still flows from the previous generation into this iteration's view; the transition timing is identical.
* **The snapshot is read-only.** Steps still mutate runner state (emit nudges, set `RunState`), but they never recompute what the view already holds. Exactly one writer of the view per iteration.

## [ ] T001 Build the view once

### Description

Define `IterationView` and compute it once per iteration (a `snapshot_step` first in the pipeline, or a runner field set at iteration start). Fold `Observations` into it and have `advance` consume it. Keep `budget_remaining`, `transcript_fullness`, `pressure`, and `undecided` pure, called only by the builder.

### Acceptance criteria

* `budget_remaining` and `transcript_fullness` each have exactly one caller (the builder).
* `grep -rn "transcript_fullness\|budget_remaining" src/pico/core/loop` shows no call inside `decision_step`, `budget_step`, or `generation_step`.
* `advance` takes the view (or a projection); no second observations struct exists.
* `make check` passes.

## [ ] T002 Read the view everywhere

### Description

Rewrite `budget_step`, `decision_step`, and `generation_step` to read `runner.view`. Replace `GenerationCompleted`'s recomputed `pressure` with the view's `pressed`.

### Acceptance criteria

* No step recomputes pressure, fullness, or remaining budget; each reads the view.
* `GenerationCompleted.pressure` is sourced from the view, and the standalone recompute in `generation_step` is gone.
* Existing loop and policy tests pass with mechanical updates only.
* `make check` passes.
