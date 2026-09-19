# Step Budgets

How many generations a run may spend is hard-coded in two places at three levels: `MAX_RUN_STEPS = 50` in `loop/runner.py` caps the root run, and `CHILD_BUDGETS = (30, 10)` in `loop/subruns.py` caps delegated children at depth 1 and depth 2 and beyond. These numbers shape everything downstream — the wind-down fraction in `loop/policy.py`, the request counter in the TUI, how much work a delegated step can attempt before "no answer within N steps" — yet experimenting with them means editing source. The right numbers almost certainly differ by model and task, which is exactly the kind of knob `.env` exists for: `LLM_TEMPERATURE` and `LLM_VISION` set the precedent. This milestone moves all three levels into one configuration value.

## Design decisions

* **One env var, one tuple.** `STEP_BUDGETS` is a comma-separated list of positive integers, e.g. `50,30,10`: the first entry caps the root run, each following entry caps the next delegation depth, and the last entry covers every depth beyond it — the same clamping `child_budget` already does with `min(depth, len(CHILD_BUDGETS))`. Unset or blank means the default `(50, 30, 10)`, so existing setups behave identically. A malformed value (non-integer, zero, negative, empty entry) raises `ConfigError` in the `LLM_CONTEXT_SIZE` style, naming the variable and the offending value.
* **`Config` gains `step_budgets: tuple[int, ...]`,** parsed in `load_config` following the existing optional-variable pattern. The default lives in one place; nothing else redefines the numbers.
* **The budgets ride on `LoopConfig`; the constants go away.** `MAX_RUN_STEPS` and `CHILD_BUDGETS` are deleted, replaced by the tuple carried in `LoopConfig`. A runner's step cap derives from its own depth against the tuple, and `run_child` inherits the parent's tuple — so `child_budget` collapses into that one lookup. `run_pico` and `headless.run_turn` build the `LoopConfig` from `config.step_budgets`; the TUI's request counter shows the root entry as it shows `max_steps` today.
* **Behaviour-preserving at the default.** With `STEP_BUDGETS` unset, every cap, wind-down threshold, TUI display, and eval run matches today's numbers exactly. Tests that construct `LoopConfig` directly keep working through the default tuple.
* **Scope guard.** No per-run or per-prompt overrides, no CLI flag, no changes to `MAX_STEP_ATTEMPTS`, `MAX_DELEGATE_DEPTH`, or the wind-down fraction, no unlimited mode beyond whatever `LoopConfig` already expresses.

## [ ] T001 STEP_BUDGETS in Config

### Description

Parse `STEP_BUDGETS` into `Config.step_budgets` with the `(50, 30, 10)` default and `ConfigError` validation.

### Acceptance criteria

* Unset and blank both yield `(50, 30, 10)`; a value like `40,20` yields `(40, 20)`.
* Non-integer, zero, negative, and empty entries each raise `ConfigError` naming `STEP_BUDGETS` and the raw value.
* `make check` passes.

## [ ] T002 Budgets through the loop

### Description

Carry the tuple on `LoopConfig`, derive each runner's cap from its depth, delete `MAX_RUN_STEPS` and `CHILD_BUDGETS`, and thread `config.step_budgets` from `run_pico` and `headless.run_turn`.

### Acceptance criteria

* A root run stops at the first entry; children at depth 1 and depth 2 stop at the second and third; depths past the end of the tuple clamp to the last entry, covered by a test with a short tuple.
* With the default tuple, root cap, child caps, wind-down onset, and the TUI's displayed maximum all match today's values.
* Neither `MAX_RUN_STEPS` nor `CHILD_BUDGETS` exists anywhere; the numbers appear only in the `Config` default.
* `make check` passes.

## [ ] T003 Document the knob

### Description

Add `STEP_BUDGETS=` to `.env.example` and a sentence to the README's Usage section explaining the format and default.

### Acceptance criteria

* `.env.example` lists `STEP_BUDGETS=` alongside the other optional variables.
* The README Usage section states the comma-separated format, that entries map to root then successive delegation depths with the last covering deeper ones, and the `50,30,10` default.
* `make check` passes.
