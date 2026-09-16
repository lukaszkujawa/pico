# Stable Fact Identity

A fact's identity is currently positional, and it is computed in two unrelated places that happen to agree. `facts()` (`src/pico/core/ledger.py`) enumerates non-error `ToolCallRecorded` events and assigns `index=len(result)` as it goes. `render_messages` (`src/pico/core/context.py`) never calls that function per message — it builds its own parallel iterator (`fact_indices = iter(fact.index for fact in facts(session))`) and advances it once per non-error tool message while walking the message list, trusting that both walks visit the same events in the same order. They do today, because both skip `is_error` events identically — but nothing pins that invariant, no test would catch a drift, and the failure mode is silent: handle summaries would name one fact while `answer` citation validation (`src/pico/core/loop.py`, the `known = {fact.index ...}` check) validates against another. Meanwhile `Session.messages()` (`src/pico/session/session.py`) invents a *third* numbering, using the event's position in `enumerate(self.events())` as the `tool_call_id`.

The store already has the right identity sitting unused: every event row carries a per-session, monotonically increasing `seq` (`src/pico/session/store.py`) that never shifts no matter how history is reinterpreted. This milestone makes `seq` the single id for an event, its tool call, and its fact — one numbering, assigned at append time, shared by the ledger, the context renderer, and citation validation. This is groundwork for `0020` (recovering a fact by id requires the id to mean one thing).

## Design decisions

* **Expose `seq`, change no schema.** Add `Session.records() -> Iterator[tuple[int, SessionEvent]]` yielding `(seq, event)` straight from the existing query; reimplement `events()` as `(event for _, event in self.records())`. No migration, no new columns.
* **`Fact.id`, not `Fact.index`.** Rename the field — it is no longer a position. `facts()` builds each `Fact` from `records()`, taking `id` from the event's `seq`. Ids are stable but not contiguous (error calls and non-tool events consume seqs); nothing may assume density.
* **`tool_call_id` is the seq.** `Session.messages()` uses `str(seq)` from `records()` as the `ToolCall`/`ToolResult` id for each `ToolCallRecorded` pair, replacing the `enumerate` position. The id is opaque to the LLM layer, so this changes nothing downstream.
* **The parallel iterator dies.** `render_messages` derives the fact id for a tool message directly from `int(message.tool_result.tool_call_id)` — the id travels with the message, so there is nothing left to keep aligned. `render_tool_result` takes the id as before, just under its honest meaning.

## [X] T001 `Session.records()` and seq-based message ids

### Description

In `src/pico/session/session.py`: add `records()` as described (select `seq, kind, payload` ordered by `seq`), rewrite `events()` on top of it, and switch `messages()` to use `str(seq)` as the tool call id for `ToolCallRecorded` pairs.

### Acceptance criteria

* `tests/session/test_session.py` covers: `records()` yields seqs matching append order starting at 1; `events()` still yields the same events as before; `messages()` gives each tool-call pair a `tool_call_id` equal to the originating event's seq (assert against a session where a non-tool event sits between two tool calls, so position and seq visibly differ); child sessions number independently from their parent.
* Fully annotated, passes strict Pyright.

## [X] T002 Seq-based fact ids in the ledger

### Description

In `src/pico/core/ledger.py`: rename `Fact.index` to `Fact.id` and populate it from `records()`. Update `answer` citation validation in `src/pico/core/loop.py` to build its known set from `fact.id`. Update any other `Fact.index` references (`src/pico/core/__init__.py` re-exports, TUI fact counting if it consumes ids).

### Acceptance criteria

* `tests/core/test_ledger.py` covers: fact ids equal the seq of their source events; interleaved error tool calls leave surrounding fact ids unchanged (only the gaps grow); an empty session yields no facts.
* `tests/core/test_loop.py` covers: an `answer` citing a valid seq-based fact id is accepted; citing a seq that exists but belongs to an error call (or no call) is rejected with the existing unknown-citation error.
* Fully annotated, passes strict Pyright.

## [X] T003 Context renderer reads ids from messages

### Description

In `src/pico/core/context.py`: delete the `fact_indices` parallel iterator from `render_messages` and take each tool message's fact id from `int(message.tool_result.tool_call_id)`. Rename `render_tool_result`'s `fact_index` parameter to `fact_id` and keep its output naming that id.

### Acceptance criteria

* `tests/core/test_context.py` covers: under budget pressure, each handle summary names exactly the fact id that `facts()` reports for that same tool call — asserted on a session containing interleaved error calls and plain assistant/user turns, the shape that would have silently broken the old parallel walk; error tool results are still never truncated.
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Confirm no `Fact.index` or enumerate-based `tool_call_id` references remain (`grep -rn "fact.index\|fact_index" src tests` should show only intentional survivors, ideally none).

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
