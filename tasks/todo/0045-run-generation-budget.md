# Run Generation Budget

Live run 2026-09-17_15-32-14 would never have ended: top-level runs have `max_steps=None`, and a model that keeps taking varied-enough actions can loop forever without tripping stuckness. Delegates already have a step ceiling; the top level needs one too — with a wind-down phase, because a run that has spent its budget still holds facts worth an answer, and stopping cold wastes them.

## Design decisions

* **Every run has a generation budget.** `LoopConfig.max_steps` stops being optional-by-default: the top-level config gets `MAX_RUN_STEPS` (generous, e.g. 100); delegates keep `MAX_DELEGATE_STEPS`. Headless and eval runs inherit the same default.
* **A soft threshold winds the run down.** At a fixed fraction of the budget (e.g. 80%), the runner injects a standing nudge: the budget is nearly spent — stop exploring, complete or prune the plan, and finish with `answer` using the facts gathered. The nudge states the remaining generations so the model can pace itself.
* **The hard limit is a truthful failure.** Exhausting the budget ends the run with an explicit error ("run stopped: generation budget of N exhausted") through the existing `fail` path — visible in the TUI, recorded like any other run failure, never silent.
* **Everything is recoverable.** The session, facts, and plan survive; the user can send a follow-up message and the next run continues from the ledger. That is the vision's recoverability applied to time, not just context.
* **No new configuration surface.** Constants in `core/loop.py`; callers that need a different budget (delegates, tests) already pass their own `LoopConfig`.

## [ ] T001 Budget with wind-down

### Description

Set the top-level default budget, add the soft-threshold wind-down nudge with remaining-generation count, and make budget exhaustion fail the run explicitly instead of ending the loop silently.

### Acceptance criteria

* A run that reaches the soft threshold receives the wind-down nudge stating the remaining generations; an `answer` after it ends the run normally.
* A run that exhausts the budget ends with the explicit budget error via `RunFinished(error=...)`, never a silent stop; the session remains resumable and a follow-up user message starts a fresh run with the ledger intact.
* Delegate behaviour is unchanged: `MAX_DELEGATE_STEPS` still applies, and a delegate hitting it still returns the existing failure text to its parent.
* Runs that answer before the soft threshold see no nudge and no behaviour change.
* `make check` passes.
