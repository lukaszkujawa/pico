# Subrun Budget and Naming

`CHILD_BUDGETS[runner.depth]` (subruns.py) indexes a fixed 2-tuple by depth. It is safe only because `depth >= MAX_DELEGATE_DEPTH` guards every spawn site — but that guard lives in two other functions (`spawn_delegate`, `step_orchestration_step`). Raise the depth ceiling and forget to extend the tuple and it is an `IndexError` mid-run, not a clean rejection: a value bounded in one file indexing a container sized in another. Alongside it, the sub-run vocabulary sprawls — `run_child` is the primitive, but `spawn_delegate`, `run_delegate` (in actions), and `step_orchestration_step` circle the same "run a sub-agent and interpret its result" idea with inconsistent name shapes (verb-noun beside noun-noun-noun for siblings that do the same shape of work). This is a small, self-contained hardening-and-naming pass, independent of 0060/0061.

## Design decisions

* **`child_budget(depth) -> int`** replaces tuple indexing: a function that cannot go out of range — it returns the last tier for any depth at or past the floor, or asserts the depth is spawnable. The budget for a depth is defined in one place and safe regardless of what the depth ceiling becomes.
* **Sibling sub-run functions share a name shape.** The two callers of `run_child` that spawn-and-conclude read as siblings: pick one shape (verb-noun) so a reader sees they are the same kind of thing. `run_child` stays the primitive; `conclude` stays. `step_orchestration_step` is a `Step` and keeps that suffix, but the helper it calls to run and interpret a step child is named symmetrically with the delegate one.
* **No behaviour change.** Budgets `(30, 10)`, `MAX_DELEGATE_DEPTH = 2`, and the depth floor are unchanged; this guards the indexing and aligns names, nothing more.
* **Scope guard.** No new sub-run kinds, no depth-ceiling change — just make the existing budget lookup total and the existing names consistent.

## [X] T001 Total budget lookup

### Description

Replace `CHILD_BUDGETS[runner.depth]` with `child_budget(depth)` that is defined for every depth a child can run at and does not raise for an out-of-tuple depth.

### Acceptance criteria

* No indexing of a depth-sized tuple by `runner.depth`; the lookup is a function.
* A test covers the budget for each spawnable depth and that the lookup is total (no `IndexError`) past the current tuple length.
* `make check` passes.

## [X] T002 Consistent sub-run names

### Description

Align the names of the spawn-and-conclude helpers so siblings share a shape; leave `run_child` and `conclude` as the shared primitives.

### Acceptance criteria

* The delegate and step sub-run helpers read as siblings (one name shape).
* No public function in the package shares a name with another (the `run_delegate` collision across modules stays resolved).
* `make check` passes.
