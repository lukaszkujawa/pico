# Generation Step Does One Job

`generation_step` runs four jobs and the seams show. It flips `WindingDown → LastWords` (a lifecycle transition that belongs to the run, not to prompting), assembles the prompt, derives pressure (removed by 0060), streams, then unpacks a `Recorded` struct back into five separate runner mutations by hand — `pressed`, `nudge`, `failure`, `actionless`, `outcome` fanned out one field at a time. That final block is the worst of both worlds: `record` returns a data class *and* its only caller manually re-scatters the fields, so `Recorded` is a struct that leaks its internals. And `record`'s own signature lies about its inputs — `record(generation, dying, undecided, actionless)` takes two booleans the caller computes from runner state one line earlier, the classic shape of a function extracted for testing and never given real inputs. This milestone follows 0060 (which removes the pressure recompute) and finishes the job: one transition owner, one uniform way to apply what a generation produced.

## Design decisions

* **`record` takes real inputs, returns commands — or is inlined.** Either `record(generation, state, session) -> Recorded` derives `dying`/`undecided` itself, or `record` folds into `generation_step`. No more passing pre-computed booleans the caller just derived. Decide by which reads cleaner once 0060 has thinned the step; a five-line `record` with real inputs is fine, a struct unpacked by hand is not.
* **`Recorded` is applied uniformly, not scattered.** If `record` stays, its result is a set of commands the step executes in one place (mirroring `advance`'s `Command`), not five `if recorded.x is not None` mutations. `Recorded.pressed` is renamed `narration_pressure` to match the state it sets — the name says what it does.
* **The `WindingDown → LastWords` transition leaves prompt assembly.** It is a run-lifecycle transition (0057's machine) and belongs with the other state transitions at the iteration edge — `snapshot_step` from 0060 or a dedicated lifecycle point — not as the first line of the function that builds a prompt. `generation_step` should assemble, stream, and record; nothing else.
* **`assemble` already takes explicit inputs (0059).** Keep it that way; this milestone does not touch prompt.py beyond removing the caller's lifecycle flip.
* **Behaviour-preserving.** `MAX_ACTIONLESS_GENERATIONS`, `NO_ACTION_NUDGE`, and the actionless/undecided/dying decision table keep their values and order. Only where the transition happens and how the result is applied change.

## [X] T001 Move the lifecycle transition out

### Description

Relocate `WindingDown → LastWords` to the iteration edge with the other `RunState` transitions. `generation_step` no longer mutates `RunState` before assembling the prompt.

### Acceptance criteria

* `generation_step` contains no `RunState` assignment before `assemble`.
* The transition still fires exactly once, on the generation that becomes last words, demonstrated by an existing or added scripted-LLM test.
* `make check` passes.

## [X] T002 Record takes inputs and applies uniformly

### Description

Give `record` real inputs (state + session) or inline it. If it stays, return commands applied in one place; rename `pressed` to `narration_pressure`.

### Acceptance criteria

* `record` takes no boolean the caller derived from runner state in the same breath.
* The five-way manual unpack in `generation_step` is gone — the result is applied uniformly or inlined.
* No field named `pressed`; the narration-pressure flag is named for what it sets.
* Loop tests pass with mechanical updates only.
* `make check` passes.
