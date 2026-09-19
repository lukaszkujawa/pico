# Named Outcomes

In a codebase whose style bans comments, signatures are the documentation — and the loop's most-travelled value is anonymous. `(text, is_error)` tuples flow through `conclude`, `spawn_delegate`, `spawn_step`, `_failed`, and `ActionResult`, where `text` sometimes means an answer, sometimes an error, sometimes a partial result, and only position says which flag is which. `_record` in dispatch does isinstance-juggling to tell an `AnswerOutcome` from a bare tuple. Alongside it, one name collision: `Recorded` in `generate.py` is the applied outcome of a generation, but "record" already means session records and `record.py`'s ceremony — three meanings for one word.

## Design decisions

* **One frozen outcome dataclass** replaces the `(text, is_error)` tuple everywhere it crosses a function boundary in `loop/`: `conclude`, the two spawn helpers, `_failed`, and the tuple half of `ActionResult`. Fields named for what they are; construction sites read as sentences. `AnswerOutcome` stays — it carries answer-specific payload — but dispatch distinguishes the two by type with less juggling.
* **`Recorded` is renamed** to say what it is without reusing "record" or "step". The rename is mechanical; no field or behaviour changes beyond it.
* **The dataclass pays for itself.** Net line count of `src/pico/core/loop/` must not grow; the isinstance ceremony it deletes is the budget.
* **Behaviour-preserving.** No message text, event, or ordering changes; tests update mechanically.
* **This milestone follows 0067.**

## [ ] T001 One outcome type

### Description

Introduce the frozen outcome dataclass and thread it through subruns and dispatch in place of `tuple[str, bool]`.

### Acceptance criteria

* No public function in `loop/` returns or accepts `tuple[str, bool]`.
* `_record`'s type discrimination shrinks; existing loop tests pass with mechanical updates.
* Net line count of `src/pico/core/loop/` is not higher than before the milestone.
* `make check` passes.

## [ ] T002 Rename Recorded

### Description

Rename `Recorded` in `generate.py` to a standalone name that reuses neither "record" nor "step"; update references.

### Acceptance criteria

* `grep -n "Recorded" src/pico/core/loop/generate.py` matches nothing.
* `make check` passes.
