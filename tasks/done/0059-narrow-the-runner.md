# Narrow the Runner

`runner: LoopRunner` appears in 23 signatures across the loop package. It is the right argument for step functions — that is the `Step` contract — but it leaks into every helper: `winding_down`, `undecided`, `pressure`, `restriction` in policy; `stream`, `record` in generate; the dispatch internals. Each helper reads two to four fields yet receives the whole mutable world, so nothing's real inputs are visible from its signature, and testing anything means building a runner.

## Design decisions

* **The rule**: only functions of type `Step` (and `LoopRunner` methods) take the runner. Every other function takes exactly the values it reads and returns values or commands instead of mutating.
* **Policy** is finished by milestone 0058 — `advance` already takes an observations snapshot. This milestone extends the same shape outward:
  * `winding_down(iterations, max_steps, depth, dying) -> bool` and `pressure(...)` take scalars or join the observations snapshot; `undecided` derives from the session in the snapshot builder.
  * `prompt.assemble` splits into a pure part (messages + specs from session, restriction, sizes) and the step-edge mutation of `active_restriction` and `last_words`.
  * `generate.stream` keeps runner access for bus and cancel — it is effectful by nature — but `generate.record` returns an outcome plus follow-up commands instead of reaching into `decision`/`generation` state.
  * dispatch helpers (`_dispatch`, `_failed`, `_record`) take the call, the registry or context, and return results; `dispatch_step` alone touches the runner.
* **No new abstraction layers.** No `RunnerView` protocol hierarchy, no dependency injection framework — plain arguments and frozen snapshots. If a helper needs more than five values, that is a sign it is two functions.
* **Mechanical, behaviour-preserving.** Public module surface (`__init__.py` exports, `Step`, `LoopConfig`) is unchanged; evals and headless are untouched.

## [X] T001 Policy and prompt helpers take their inputs

### Description

Convert the remaining policy helpers and `prompt.assemble`/`reconcile` to explicit inputs, with runner access confined to the step functions that call them.

### Acceptance criteria

* No function in `policy.py` or `prompt.py` outside step functions takes `LoopRunner`.
* Helper tests construct plain values, no runner instances.
* `make check` passes.

## [X] T002 Generate and dispatch helpers take their inputs

### Description

Apply the same rule to `generate.py` and `dispatch.py`: `record` and the dispatch internals become functions of their inputs returning outcomes; `generation_step` and `dispatch_step` own all mutation.

### Acceptance criteria

* In `generate.py` and `dispatch.py`, only step functions and intentionally effectful streaming take the runner.
* `grep -rn "runner: LoopRunner" src/pico/core/loop` matches only `Step`-typed functions, `stream`, and `subruns` wiring.
* Existing behaviour is preserved; loop and integration tests pass with mechanical updates only.
* `make check` passes.
