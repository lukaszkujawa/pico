# Action Components

`core/actions.py` is 492 lines and reads as four unrelated things interleaved: an argument-parsing toolkit, action dataclasses (`Shell` with its incremental reader, `Answer`, `Delegate`, `ResultShape`), a 110-line `_TOOL_SPECS` dict, and the `*_tool` factories that finally pair a spec with an executor. To understand one action — say `set_plan` — a reader visits three places: its dataclass near the top, its spec in the middle of the dict, and its factory at the bottom. Meanwhile the other half of the action vocabulary lives in `core/loop/builtins.py`: the four runner-bound actions (`shell`, `answer`, `search_facts`, `delegate`) with their own spec-beside-executor style and the `ActionContext` they run against. Milestone 0049 flagged this split as deferred work; this milestone finishes it.

The target: one `core/actions/` package where each action is a self-contained component — its spec, argument parsing, and execution side by side in one module, readable top to bottom without visiting anywhere else — and one catalog that assembles them into the registry and vocabulary. `core/loop/builtins.py` disappears. The measure of success is that opening any action module answers every question about that action, and nothing else.

## Design decisions

* **Two action kinds remain, stated by their types.** A `Tool` executes from arguments alone (`Mapping -> str`); a `RunnerAction` needs the run around it and executes against `ActionContext` (`(ActionContext, Mapping) -> ActionResult`). No unifying abstraction over the two — the difference is real, and a shared base class would only blur it.
* **Modules group by domain, not by kind.** `files.py` (read_file, write_file), `shell.py` (the `Shell` command runner with its incremental reader, plus both the plain tool and the streaming runner action), `scratch.py` (load_table, sql), `facts.py` (note, read_fact, search_facts), `planning.py` (set_plan, complete_step), `answer.py` (`Answer`, `AnswerOutcome`, verification), `delegate.py` (`Delegate`, `MAX_DELEGATE_DEPTH`, the spawn-backed action). A domain module may hold one tool and one runner action when they share a mechanism, as `shell.py` does.
* **Within a module, each action reads as one block:** spec, then parsing, then execution — the spec is defined next to the code it describes, never in a central dict. `_TOOL_SPECS` dies.
* **The shared toolkit is two small modules.** `arguments.py`: `InvalidActionError`, `require`, and the list/fields helpers. `shape.py`: `FieldType`, `ResultShape`. Nothing else is shared.
* **`ActionContext` moves into the package** (`context.py`), since it is the actions' contract, not the loop's. `spawn` stays a plain callable, so the package never imports `core.loop` and the dependency keeps pointing loop → actions.
* **One catalog.** `catalog.py` holds `register_actions(registry, session, depth)`, `RUNNER_ACTIONS`, and `vocabulary` — the only module that knows the full roster. `core/loop/prompt.py` and `dispatch.py` import from it; `core/loop/builtins.py` is deleted.
* **The public surface is preserved.** `actions/__init__.py` re-exports what outside consumers use today (`register_actions`, `MAX_DELEGATE_DEPTH`, `InvalidActionError`, `ResultShape`, `Answer`, `Delegate`, `Shell`, `require`, …) so `app.py`, `headless.py`, and the loop package keep working during the split; exports are trimmed to real consumers at the end.
* **No behaviour changes anywhere.** Specs, descriptions, error texts, and execution semantics move verbatim; the existing tests are the proof.

## [X] T001 Package skeleton and shared toolkit

### Description

Convert `core/actions.py` into the `core/actions/` package. Extract `arguments.py` (`InvalidActionError`, `require`, the string-list, int-list, and fields helpers) and `shape.py` (`FieldType`, `ResultShape`), moving code verbatim. Everything else stays temporarily in one module inside the package, with `__init__.py` re-exporting today's full surface so no import outside `core/actions/` changes.

### Acceptance criteria

* `core/actions.py` is gone; the package imports cleanly and every consumer is untouched.
* `arguments.py` and `shape.py` each read as one small, complete toolkit.
* `make check` passes.

## [X] T002 One component per action

### Description

Split the remaining interim module into the domain modules: `files.py`, `shell.py`, `scratch.py`, `facts.py` (note and read_fact for now), and `planning.py`. In each, restructure so every action is one contiguous block — spec, parsing, execution — deleting `_TOOL_SPECS` and the `_action_tool` indirection where a direct `Tool` construction is clearer. Specs and behaviour move verbatim.

### Acceptance criteria

* Each module is under ~120 lines and covers exactly its domain; no central spec dict remains.
* Reading any single module answers what the action accepts, validates, and does, with no cross-references.
* Existing tests pass unchanged apart from import paths kept working by `__init__.py`.
* `make check` passes.

## [X] T003 Absorb the runner-bound actions

### Description

Move `core/loop/builtins.py` into the package and delete it: `ActionContext`, `ActionResult`, `AnswerOutcome`, `RunnerAction`, and `Spawn` to `context.py`; the answer action with its verification to `answer.py`; the delegate action with `Delegate` and `MAX_DELEGATE_DEPTH` to `delegate.py`; the streaming shell action into `shell.py` beside the plain one; the search_facts action into `facts.py`. Update `core/loop/dispatch.py`, `prompt.py`, and `subruns.py` imports. The package must not import `core.loop`.

### Acceptance criteria

* `core/loop/builtins.py` no longer exists; `grep -rn "loop.builtins"` finds nothing.
* Each runner-bound action sits beside its domain peers as one spec-parse-execute block.
* No module under `core/actions/` imports `pico.core.loop`.
* `make check` passes.

## [X] T004 One catalog

### Description

Create `catalog.py` as the single roster: `register_actions`, `RUNNER_ACTIONS`, and `vocabulary` assembled from the domain modules. Trim `__init__.py` to what `app.py`, `headless.py`, `core/loop/`, and the tests actually import. Reorganise `tests/core/test_actions.py` and `test_builtins.py` along the new module lines (`test_actions_files.py`, `test_actions_shell.py`, … or one file per domain under a `tests/core/actions/` folder), moving tests verbatim and deleting none.

### Acceptance criteria

* The full action roster is visible in one screen of `catalog.py`; no other module registers or enumerates actions.
* `vocabulary` behaviour — depth filtering and restriction — is unchanged under the existing tests.
* Test count is unchanged or higher; no test file exceeds ~400 lines.
* `make check` passes.

## [X] T005 Final soundness sweep

### Description

Run `make check` over the finished milestone and fix everything it surfaces — lint, format, strict typing, dead code, `tach` boundaries, tests, build. Delete anything the split orphaned, then make a final read-through of each `core/actions/` module purely for readability: names that say what things are, one action per block, no comments, no leftover indirection.

### Acceptance criteria

* `make check` passes clean from a fresh run.
* No orphaned symbols or re-exports without a consumer remain.
* Every module in `core/actions/` reads top to bottom as a single concern.
