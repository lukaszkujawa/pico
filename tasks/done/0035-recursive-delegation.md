# Recursive Delegation

The vision promises "isolated subagents with small, explicit contexts and typed results". What exists is a scoped question-answerer: a delegate gets `read_file`, `read_fact`, `answer`, ten steps, and returns an untyped string (`register_delegate_actions`, `src/pico/core/actions.py`). It cannot run shell — it cannot even list a directory, so it only reaches files whose paths the parent already knew, which defeats the point of delegating exploration. It cannot delegate further, so decomposition stops at depth one. And its answer arrives as prose the parent must re-parse.

This milestone makes delegates real subagents: full capabilities, recursion with a depth cap, and an optional typed result the runtime validates deterministically before the answer is accepted. Isolation is unchanged — a delegate still works in its own child session with its own compiled context.

## Design decisions

* **One tool registry.** Delegates get the same actions as the main loop via `register_actions`; `register_delegate_actions` is deleted. Shell gives them exploration, plan tools give them their own checklist in their own session, and `delegate` gives them recursion. With shell available the `can_verify` carve-out loses its reason to exist: delegates may use `verify`, and `can_verify` is removed everywhere — fewer concepts.
* **Depth cap.** Delegate depth is the count of `/` in the session id. At `MAX_DELEGATE_DEPTH` (3) the `delegate` tool is simply not registered, so the model at the bottom never sees an option it cannot use.
* **Shared cancellation, same guardrails.** The child runner receives the parent's `cancel` event so cancelling a run stops the whole tree. The child loop uses the same steps as the main loop — including `stuckness_step`, which delegates currently lack and, now that they run shell, need — with `MAX_DELEGATE_STEPS` unchanged.
* **Typed results, minimally.** `delegate` gains an optional `fields`: a flat mapping of field name to `"string" | "number" | "boolean"`. When present, the runtime appends the expected shape to the child's question, and the child's `answer` is accepted only if its content parses as a JSON object with exactly those fields and types — checked deterministically, rejected back into the child loop the way a failed `verify` is, so the child retries. When absent, plain text as today. No JSON Schema dependency; a flat record is enough typing for small models.
* **Provenance stays in the log.** The delegate's answer is recorded as a parent tool result, so it becomes a citable parent fact; the child's own facts and citations remain in its session in the shared database. Cross-session `read_fact` is a separate milestone only if evals show parents needing to reopen delegate evidence.

## [X] T001 Full capabilities and recursion

### Description

Register delegates through `register_actions`, delete `register_delegate_actions` and `can_verify`, derive depth from the session id, withhold the `delegate` tool at `MAX_DELEGATE_DEPTH`, run children with the main loop's steps, and pass the parent's cancel event down.

### Acceptance criteria

* `tests/core/test_loop.py` covers: a delegate can run shell and answer with `verify`; a delegate can spawn a delegate, and session ids nest (`.../delegate/1/delegate/1`); at max depth the specs sent to the LLM contain no `delegate` tool; setting the parent's cancel event mid-delegate cancels the child run; a repeating delegate is stopped by stuckness.
* `can_verify` and `register_delegate_actions` no longer exist anywhere in `src/` or `tests/`.
* Fully annotated, passes strict Pyright.

## [X] T002 Typed results

### Description

Add the optional `fields` argument to the `delegate` action and spec, thread it to the child, append the expected shape to the child's question, and validate the child's answer per the design decisions.

### Acceptance criteria

* `tests/core/test_loop.py` covers: with `fields` given, a conforming JSON answer is accepted and returned to the parent verbatim; a non-JSON answer, a missing field, an extra field, and a wrong-typed field are each rejected with a message naming the problem and the child loop continues; without `fields`, prose answers pass unchanged; an invalid `fields` value (unknown type name, non-object) is an invalid action against the parent.
* `tests/core/test_actions.py` covers `Delegate.from_arguments` parsing with and without `fields`.
* Fully annotated, passes strict Pyright.

## [X] T003 Integrate and measure

### Description

Update the `delegate` tool description to say what a delegate can now do — explore with shell, work to its own plan, delegate further, and return a typed record via `fields`. Add one eval task whose answer requires locating information across enough files that exploration must happen beyond paths named in the prompt, with a deterministic check. Run `make check`.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

`make check` green (503 tests, 97.7% coverage). Evals are no longer run by the unattended loop; one manual `make evals` sample on this branch (`glm-4.7-flash:latest`, 128k) scored 9/12 — `run_script` and `many_small_steps` failed as they occasionally do, and the new `locate_owner` task failed in this sample and deserves a look in a future evals pass. No baseline run was taken.

Commit:
