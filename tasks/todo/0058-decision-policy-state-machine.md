# Decision Policy State Machine

`DecisionState` encodes an implicit state machine in four mutable fields (`demanded`, `demanded_at`, `crossroads`, `crossroads_generations`), with transitions scattered across `decision_step`, `demand`, `restriction`, `_degraded_ending`, and `generate.record`. Reading any one site tells you nothing about the machine; `decision_step` is a chain of guards whose meaning lives in the reader's head. Make the machine explicit.

## Design decisions

* **Named states**, one frozen dataclass each, union-typed: `Quiet`, `Demanded(cause, at)`, `Crossroads(cause, generations)`. The current boolean-and-counter soup maps one-to-one: `demanded_at` lives in `Demanded`, `crossroads_generations` in `Crossroads`, so illegal combinations become unrepresentable.
* **Pure transition function**: `advance(state, observations) -> (state, command)`. Observations is a frozen snapshot — `undecided`, `iterations`, `pressure`, `narration` — built once per step from the runner. Commands are what the caller must do: `Ask(nudge)`, `Restrict(...)`, `EndDegraded(narration | None)`, or nothing. No transition touches the runner, the bus, or the session.
* **Effects stay at the step edge.** `decision_step` builds observations, calls `advance`, stores the state, and executes the command — including the pane/bus publishing currently buried in `_degraded_ending`, which is an effect and never belonged in policy.
* **Thresholds unchanged**: `DECISION_GRACE`, `MAX_CROSSROADS`, and the nudge texts keep their values and wording. This is a restructuring, not a behaviour change.
* **`generate.record` stops mutating decision state directly.** Narration pressure becomes an observation (`NARRATION_PRESSURE` flows in via the snapshot), so the machine has exactly one driver.
* Transition tests are table-driven over `(state, observations) -> (state, command)` with no runner, bus, or session fakes.

## [ ] T001 States, observations, and transitions

### Description

Define the state union, the observations snapshot, the command union, and `advance` in `policy.py` (or a sibling module if `policy.py` splits naturally). Cover every transition with table-driven tests, including grace expiry, repeat crossroads, and both degraded endings.

### Acceptance criteria

* Every reachable transition is a case in `advance`, pure and tested without fakes.
* `DECISION_GRACE` and `MAX_CROSSROADS` behaviour is bit-for-bit identical to today, demonstrated by tests mirroring the current sequences.
* `make check` passes.

## [ ] T002 Drive the machine from the step

### Description

Rewrite `decision_step` to snapshot observations, advance the machine, and execute the returned command. Delete `demand`, `record_narration` mutation from `generate.record`, `_degraded_ending`, and the old `DecisionState` fields.

### Acceptance criteria

* `decision_step` is the only writer of decision state; `restriction` reads the current state instead of a boolean flag.
* The degraded-ending bus publishing happens in the step, not in policy.
* Existing loop tests pass unchanged or with mechanical updates only.
* `make check` passes.
