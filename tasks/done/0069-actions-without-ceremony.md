# Actions Without Ceremony

Four action dataclasses exist for one statement each. `ReadFile` and `WriteFile` (actions/files.py) are frozen dataclasses with `from_arguments` and `execute` methods, wrapped in closure factories — four layers around `open().read()` and `open().write()`, constructed in exactly one place and dead one line later. `SetPlan` and `CompleteStep` (actions/planning.py) are the same shape: `from_arguments` is a single `require*` call, the instance is consumed immediately. The parse-to-dataclass pattern earns its keep where the value crosses a boundary or carries validation state — `Delegate` travels through `context.spawn`, `Answer` is re-rendered in citation rejections — but a dataclass that lives for one statement is ceremony. `note_tool` (actions/facts.py) already shows the house shape: parse, act, return, one closure. Alongside this, `require_str_list` and `require_int_list` (actions/arguments.py) are the same seventeen lines twice, differing only in element type and one stowaway rule: the str version rejects empty lists (a `set_plan` business rule hiding in a generic parser), the int version allows them (empty `citations` is legal).

## Design decisions

* **Inline the four one-statement dataclasses.** `read_file_tool`, `write_file_tool`, `set_plan_tool`, and `complete_step_tool` become single closures in the `note_tool` shape: parse arguments, act, return the result string. Error handling (`ToolError` on `OSError`, etc.) and result texts are unchanged.
* **`Delegate` and `Answer` stay.** They are the pattern earning its keep; nothing about them changes.
* **One generic list parser.** `require_str_list` and `require_int_list` collapse into `require_list[T](arguments, field, element: type[T]) -> tuple[T, ...]` beside `require[T]`. Error messages keep the same shape (list check, element-type check).
* **The empty-check moves to its owner.** `require_list` does not reject empty lists; `set_plan_tool` raises `InvalidActionError("field 'steps' must not be empty")` itself, where a reader of the plan rules looks for it. `citations` keeps accepting the empty list, as today.
* **Pure deletion.** No tool spec, description, error message, or result text changes; net line count of `src/pico/core/actions/` decreases by roughly seventy lines. Tests update mechanically (imports and any direct dataclass construction).

## [X] T001 Inline the one-statement dataclasses

### Description

Rewrite the four tool factories in files.py and planning.py as single closures; delete `ReadFile`, `WriteFile`, `SetPlan`, and `CompleteStep`.

### Acceptance criteria

* `grep -rn "class ReadFile\|class WriteFile\|class SetPlan\|class CompleteStep" src/` matches nothing.
* Tool result strings and error messages are byte-identical to before, shown by existing tests passing with import-level updates only.
* `make check` passes.

## [X] T002 One list parser

### Description

Replace `require_str_list` and `require_int_list` with a generic `require_list`; move the non-empty rule into `set_plan_tool`.

### Acceptance criteria

* `arguments.py` has one list parser; neither old name exists anywhere under `src/`.
* An empty `steps` list is still rejected with the same message; an empty `citations` list is still accepted.
* Net line count of `src/pico/core/actions/` is lower than before the milestone.
* `make check` passes.
