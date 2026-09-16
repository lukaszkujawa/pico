# Unique Pane Identity

This is the root cause of the corrupted transcript in the attached screenshot, and it must be fixed first because every other rendering fix is invisible until the pane-mounting thread stops dying.

Pane widget ids are minted from two independent, colliding sources.

`LoopRunner.new_id()` (`src/pico/core/loop.py`) draws text and thinking pane ids from an `itertools.count()` shared across the whole process (`id_source` is created once in `src/pico/app.py:71` and reused for every turn). Tool call panes do not use it at all: `tool_call_step` publishes `ToolCallStarted(id=call.id, ...)`, where `call.id` comes from the LLM. `OllamaClient._parse_lines` (`src/pico/llm/ollama.py:91`) initialises `next_call_id = 0` as a local at the top of **each stream** and falls back to it whenever the model omits an id — which local Ollama models do essentially always.

So the second tool call of a session is published with `id="0"` again. `PicoApp.on_tool_call_pane_create` mounts `ToolCallPane(id="tool-0")` into a conversation that already contains `tool-0`, and Textual raises:

```
textual._node_list.DuplicateIds: Tried to insert a widget with ID 'tool-0',
but a widget already exists with that ID (ToolCallPane(id='tool-0'))
```

Confirmed by direct reproduction against the real app. The raise happens on the message pump while handling a posted message, so the failure is not contained: pane mounting stops behaving coherently for the remainder of the run, which is why the screenshot shows boxes out of order, an answer rendered as a raw-JSON error box, and a spinner stranded above the transcript.

The same collision exists between namespaces (`assistant-0` vs a thinking pane) and across sessions — `action_new_session` clears the pane dictionaries but the ids keep counting from wherever they were.

## Design decisions

* **One id source for every pane.** The runtime, not the model, owns pane identity. `tool_call_step` mints its pane id with `runner.new_id()` exactly like text and thinking panes do, and passes it as the event's `id`. The model's `call.id` stays on `ToolCall` for the transcript/session record but never reaches a widget id.
* **Keep the two ids distinct in the event.** `ToolCallStarted`/`ToolCallFinished` already carry `tool_call`; the `id` field becomes unambiguously "the pane id" rather than "whatever the model said". A pane id must pair a `ToolCallStarted` with its `ToolCallFinished`, so `tool_call_step` mints the id once per call and reuses it for both publishes.
* **Ids are per-process and monotonic, never reset.** `action_new_session` clears panes but must not reset the counter; a stale in-flight event from a cancelled run must never collide with a fresh pane.
* **Fix the Ollama fallback too.** `next_call_id` moves to instance state (or an `itertools.count` on the client) so a model that omits ids does not hand back duplicate `ToolCall.id` values within one session. Duplicate tool-call ids are a correctness problem for the session record independent of the TUI.

## [X] T001 Runtime-owned tool call pane ids

### Description

In `src/pico/core/loop.py`'s `tool_call_step`, mint a pane id with `runner.new_id()` for each call and use it for both the `ToolCallStarted` and `ToolCallFinished` publishes, instead of `call.id`.

### Acceptance criteria

* `tests/core/test_loop.py` covers: two tool calls in one turn publish two different `ToolCallStarted.id` values even when both `ToolCall.id`s are identical (`"0"`); a call's `ToolCallStarted.id` and `ToolCallFinished.id` match each other; pane ids do not collide with the ids minted for text/thinking panes in the same run.
* Fully annotated, passes strict Pyright.

## [X] T002 Non-repeating tool call ids from Ollama

### Description

In `src/pico/llm/ollama.py`, move `next_call_id` out of `_parse_lines`'s local scope so the fallback id does not restart at `0` for every stream.

### Acceptance criteria

* `tests/llm/test_ollama.py` covers: two successive `stream()` calls whose chunks omit tool-call ids yield `ToolCall.id` values that differ across the streams, not `"0"` twice; an explicit id in the payload is still used verbatim.
* Fully annotated, passes strict Pyright.

## [X] T003 Regression test for the duplicate-id crash

### Description

Add a TUI test that drives `PicoApp` with two tool calls sharing the model's `ToolCall.id` and asserts both panes mount. This is the regression guard for the crash above; it must fail against the current `main` behaviour of passing `call.id` through as the pane id.

### Acceptance criteria

* `tests/tui/test_app.py` covers: publishing `ToolCallStarted`/`ToolCallFinished` for two calls with distinct pane ids mounts two `ToolCallPane`s and raises no `DuplicateIds`; a second turn after `action_new_session` mounts panes without colliding with the previous session's ids.
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until green.

### Acceptance criteria

* `make check` passes, including the coverage floor.

### Completion

Commit:
