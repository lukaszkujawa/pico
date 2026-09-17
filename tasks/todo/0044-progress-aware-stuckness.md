# Progress-Aware Stuckness

Live run 2026-09-17_15-32-14: 62 generations of alternating re-reads sailed past the stuckness assessor because it only sees *consecutive* identical calls, and the plan sat with zero steps checked an hour after step one was demonstrably done — `complete_step` was never called, so the run's own progress signal read as permanently unfinished. The assessor is the designed home for deterministic progress judgement; it needs to measure progress, not just literal repetition.

## Design decisions

* **Repetition is counted over the whole turn, not just the tail.** For calls since the last user message, the assessor counts occurrences of each identical `(name, arguments)` pair. A pair seen twice draws a nudge that names the existing fact id: "you already ran read_file(...) — its result is fact N; use read_fact(N) or do something new". A pair repeated `STUCK_THRESHOLD` times stops the run, exactly as consecutive repetition does today.
* **The existing streak rules stay.** Consecutive-repeat and consecutive-failure thresholds are unchanged; the windowed count is a third signal beside them, and the most urgent signal wins the single nudge slot.
* **A stagnant plan is a progress signal.** When a plan has unfinished steps and neither `set_plan` nor `complete_step` has been recorded within the last `PLAN_STALL_GENERATIONS` assistant generations, the nudge reminds the model to either complete the step it has finished or revise the plan, quoting the first unfinished step. Stale plans nudge; they never stop the run — termination belongs to the run budget.
* **Deterministic and derived.** Everything is computed from session events in `core/stuckness.py`; no model judgement, no new state, no new events.

## [ ] T001 Windowed repetition and plan-stall signals

### Description

Extend `assess` in `core/stuckness.py` with the per-turn repeated-call count (nudge naming the fact id, stop at threshold) and the plan-stall nudge; wire the fact-id lookup from the ledger.

### Acceptance criteria

* A turn with `read_file(a)`, `read_file(b)`, `read_file(a)` yields a nudge naming `read_file(a)`'s existing fact id; the same pair repeated `STUCK_THRESHOLD` times, interleaved with other calls, stops the run with a reason naming the call.
* Non-identical calls never trip the windowed signal; a new user message resets the window.
* A plan with unfinished steps and no plan event for `PLAN_STALL_GENERATIONS` generations yields a nudge quoting the first unfinished step; recording `complete_step` clears it; a plan with all steps done never nudges.
* Streak-based nudges and stops behave exactly as before when no windowed signal fires.
* `make check` passes.
