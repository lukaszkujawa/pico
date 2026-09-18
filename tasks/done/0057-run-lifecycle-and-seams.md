# Run Lifecycle and Loop Seams

The run's life is smeared across four mutable runner fields — `error`, `final_answer`, `dying_of`, `last_words` — with the legal combinations living in the reader's head: `execute` checks `last_words and final_answer is None`, dispatch ends with `"done" if runner.last_words else outcome`, three policy steps bail on `dying_of`, and `_degraded_ending` writes `final_answer` directly, bypassing the answer action. `prompt.assemble` flips `last_words` as a side effect of building a prompt. This is the same disease 0058 diagnoses in `DecisionState`, in a bigger organ — so this milestone runs first, letting 0058 formalise the decision machine as a sub-state of an already-explicit lifecycle. The remaining items are seam bugs the review surfaced: two `run_delegate`s that disagree about degraded answers, a one-slot signal mailbox that silently eats nudges, and a `next()` in stuckness that can kill a run to decorate a hint.

## Design decisions

* **One state union, one writer.** `RunState = Running | WindingDown(cause) | LastWords(cause) | Answered(content) | Failed(reason)`, frozen dataclasses, replacing all four fields. Transitions happen only in step functions and `execute`; everything else pattern-matches on the current state. Illegal combinations become unrepresentable.
* **`assemble` stops deciding the run is over.** The `Running → LastWords` transition moves to the step edge that owns it; prompt assembly reads the state, never writes it.
* **One child conclusion.** A single `conclude(child) -> (text, is_error)` interprets a finished child runner for both `step_orchestration_step` and delegate spawning. A child that answered under `LastWords` reports `partial — {cause}` in both paths — today delegate returns a degraded guess as clean success, which is a behaviour bug this milestone fixes. `subruns.run_delegate` gets a name that stands apart from `actions.delegate.run_delegate`.
* **Signals accumulate, they don't clobber.** `pending_signal` becomes a list of nudges joined into the prompt; `Restrict` keeps its precedence explicitly rather than by accident of step order.
* **Nudges never kill runs.** `_existing_fact_id` takes a default and the windowed nudge is skipped when the fact is missing, instead of StopIteration falling into `execute`'s blanket except.
* **Thresholds, wording, and public surface unchanged** apart from the delegate partial fix. Mechanical elsewhere.

## [X] T001 RunState union

### Description

Define `RunState` and replace `error`, `final_answer`, `dying_of`, `last_words` on the runner. Rewrite `execute`, the step guards, `_degraded_ending`, and `assemble` to read the state; confine writes to step edges and `execute`.

### Acceptance criteria

* The four fields are gone; `grep -rn "dying_of\|last_words" src/pico` matches nothing.
* `prompt.assemble` performs no runner mutation beyond `active_restriction` (which 0059 then removes).
* Every terminal outcome — answered, budget-spent, stuck, degraded, failed, cancelled — is a named state, demonstrated by tests driving the loop with a scripted LLM.
* `make check` passes.

## [X] T002 One child conclusion

### Description

Extract `conclude(child)` shared by step orchestration and delegate spawning, marking `LastWords` answers as partial in both. Rename the subruns-level delegate helper.

### Acceptance criteria

* Step and delegate interpret child runners through the same function; a delegate that dies of budget returns `partial — …`, covered by a test.
* No two public functions in the package share a name.
* `make check` passes.

## [X] T003 Signal list and nudge hardening

### Description

Replace the one-slot mailbox with an accumulating list of nudges; make `Restrict` precedence explicit. Give `_existing_fact_id` a `None` default and skip the windowed nudge when it misses.

### Acceptance criteria

* Two nudges emitted in one iteration both reach the prompt, covered by a test.
* A missing repeat-fact yields no nudge and no run failure.
* `make check` passes.
