# Files Beside Their Consumers

Three small placement wrongs and one piece of dead generality. `stuckness.py` sits at `core/` top level but has exactly one consumer, the loop's policy — a file with one consumer lives next to that consumer. `signals.py` is 13 lines holding two dataclasses (`Nudge`, `Restrict`) that only the loop uses; `errors.py` is six lines holding two exceptions, one of which is raised in exactly one place (`tools.py`). Neither module is a concept; both are parking spots. And `render_tool_result` takes a `level` parameter with one live value — every call site passes `"handle"`, and the `"full"` branch returns its input unchanged. Defensive design for a variation that doesn't exist.

## Design decisions

* **`stuckness.py` moves into `loop/`.** Its vocabulary (assess, nudges, thresholds) is policy-rule vocabulary; after 0065 it sits beside the rules that call it. Move, don't rewrite.
* **`signals.py` folds into `loop/state.py`.** `Nudge` and `Restrict` join the loop's other value types. The module dies.
* **`errors.py` folds into `tools.py`.** `UnknownToolError` is raised there; `ToolError` is the actions' contract with dispatch and travels along. The module dies; importers update.
* **`RenderLevel` and `level` go.** `render_tool_result` becomes the handle renderer it actually is; if the merged prompt module (0066) already renamed or relocated it, apply the same deletion there.
* **Mechanical throughout.** No behaviour, text, threshold, or event changes; the diff is moves, import updates, and one parameter deletion. Net line count of `src/pico/core/` decreases.
* **This milestone follows 0066** (the merge decides where the renderer lives before this deletes its parameter).

## [X] T001 Moves and folds

### Description

Move `stuckness.py` into `loop/`; fold `signals.py` into `loop/state.py` and `errors.py` into `tools.py`; update importers.

### Acceptance criteria

* `core/` top level contains only `actions/`, `loop/`, `bus.py`, `events.py`, `ledger.py`, `scratch.py`, `search.py`, `tools.py`.
* `signals.py` and `errors.py` no longer exist anywhere under `src/`.
* `docs/core.md`'s package map reflects the new layout (no core-level `stuckness.py`, no `signals.py`, no `errors.py`).
* `make check` passes.

## [X] T002 Drop the dead level

### Description

Delete `RenderLevel` and the `level` parameter; the tool-result renderer renders handles, full stop.

### Acceptance criteria

* `grep -rn "RenderLevel\|level=" src/pico/core` shows no render-level plumbing.
* Handle text produced for the prompt is byte-identical to before.
* `make check` passes.
