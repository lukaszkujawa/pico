# The core loop

`LoopRunner.execute()` runs one iteration after another until a phase ends the
run. Each iteration walks the phases in `DEFAULT_LOOP_STEPS`; a phase is a
`Step` — a callable that takes the runner and returns `"continue"`, `"done"`,
or `"cancelled"`. Tests may script a custom tuple of steps, and children
inherit their parent's tuple.

## Iteration pipeline

```
execute()
  │  generation budget spent → WindingDown        (hard stop, owned by the skeleton)
  ▼
policy_step
  │  stuckness_rule                               (stuck → WindingDown, or a nudge)
  │  observe() → IterationView                    (pure snapshot of the situation)
  │  budget_rule                                  (80% nudge + budget pressure)
  │  decision_rule                                (advance() the decision machine)
  ▼
step_orchestration_step                           (maybe run a plan step in a child)
  ▼
generation_step
  │  WindingDown → LastWords                      (owned by the skeleton: a wind-down
  │                                                becomes last words in the same iteration)
  │  talk to the LLM, record what came back
  ▼
tool_call_step                                    (act on the world)
```

## Policy rules

The policy phase runs three rules of one shape: each reads the runner and
returns a `Verdict` — nudges to emit, an optional pressure for the view, an
optional run-state transition, an optional decision-state transition, and an
optional degraded answer. No rule mutates the runner; `apply_verdict` applies
every verdict in one place.

* **stuckness_rule** assesses the session for repeated tool calls. Stuck runs
  wind down; near-stuck runs get a nudge.
* **budget_rule** is the budget's one soft voice: past 80% of the generation
  budget it produces both the wind-down nudge and the pressure that feeds the
  decision machine. The hard stop stays in `execute()`.
* **decision_rule** advances the decision state machine over the view: a
  pressed, undecided run is asked to decide, then restricted to a crossroads,
  then ended with a degraded answer.

`observe()` is pure: building the view twice yields the same view. The
`narration_pressure` flag it reads is owned by `generation_step`, which sets it
after a generation that narrated without acting and clears it otherwise.
