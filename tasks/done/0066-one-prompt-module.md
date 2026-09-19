# One Prompt Module

"Turn the log into a prompt" is one concept living in two files in two directories. `loop/prompt.py` holds `assemble`; `core/context.py` holds everything it calls — `compile_context`, `recency_window`, the briefing, message rendering, token math — plus things that aren't prompt compilation at all (`fact_index`, `estimate_tokens`). Eight importers each pull a different name out of `context.py`: the TUI takes `estimate_tokens`, `loop/state.py` takes `Degradation`, `actions/answer.py` takes `fact_index`. A module everyone imports for a different reason is a drawer, not a concept, and its name confirms it — "context" doesn't stand alone. Reading `assemble` means reconstructing it bottom-up across both files.

## Design decisions

* **`context.py` merges into `loop/prompt.py` and dies.** The merged module reads top-down in order of invocation: `assemble` first, then `compile_context`, `recency_window`, the demotion/rendering helpers below. Underscore-prefix everything not imported elsewhere.
* **Non-prompt residents move home.** `fact_index` renders the ledger, so it moves to `ledger.py` (with its `_index_line`/`_newest_per_call` helpers). `estimate_tokens` and `prompt_budget` are token math, so they move to `pico.llm.budget` next to `completion_reserve` — the TUI then imports token estimation from `pico.llm`, not from inside the loop.
* **No import cycles.** `loop/state.py` holds `Degradation` on `GenerationState`, and the merged `prompt.py` will import from `state`-adjacent modules; `Degradation` lands wherever the imports stay one-directional (moving it into `loop/state.py` is acceptable if `prompt.py` importing it from there stays acyclic). The implementation picks the placement; the criterion is zero cycles.
* **Pure relocation.** No prompt content, budget math, degradation behaviour, or message order changes. Tests move with their subjects; assertions change only in import paths.
* **`docs/core.md` follows.** The package map loses `context.py` and shows prompt compilation as one box inside the loop.

## [X] T001 Merge and rehome

### Description

Merge `context.py` into `loop/prompt.py` entry-point-first; move `fact_index` to `ledger.py` and `estimate_tokens`/`prompt_budget` to `pico.llm.budget`; place `Degradation` acyclically; delete `context.py`.

### Acceptance criteria

* `pico/core/context.py` no longer exists and nothing imports `pico.core.context`.
* `loop/prompt.py` opens with `assemble`; helpers follow in order of invocation; names used only inside the module are underscore-prefixed.
* The TUI imports token estimation from `pico.llm`; `import` statements in `pico/tui/` reference nothing under `pico.core.loop`.
* No import cycles (`make check` passes, which includes the linter's cycle detection if any; otherwise demonstrated by clean imports).
* `make check` passes.

## [X] T002 The map matches the territory

### Description

Update `docs/core.md`'s package map and prose: no `context.py`, prompt compilation shown as one module.

### Acceptance criteria

* `docs/core.md` contains no reference to `context.py`.
* `make check` passes.
