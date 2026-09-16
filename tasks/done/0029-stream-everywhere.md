# Stream Everywhere

Reported as: "streaming is inconsistent — some boxes render token by token and some all at once. I want streaming everywhere."

The asymmetry is real and structural. Three of the four pane kinds never stream, for three different reasons.

**Assistant and thinking panes do stream.** `stream_step` (`src/pico/core/loop.py`) publishes an `AssistantTextDelta` / `AssistantThinkingDelta` per chunk, and the panes append to a `reactive` and repaint. This is the behaviour the other panes should match.

**Tool call arguments arrive all at once.** `stream_step` explicitly discards argument deltas:

```python
case ToolCallDelta():
    pass
```

`ToolCallReady` — emitted only when the call is fully parsed — is what eventually produces `ToolCallStarted` with a complete `arguments` mapping, and `ToolCallPaneCreate` renders it as one `json.dumps` blob (`format_arguments`). For a long `write_file` content argument the box sits empty behind a spinner and then snaps to full text. `ToolCallDelta` is a defined type in `src/pico/llm/types.py` that no producer emits and no consumer handles — dead plumbing that was meant to carry exactly this.

**Tool call results arrive all at once,** unavoidably for `shell` — `Shell.run` uses `subprocess.run`, which returns only on exit. A 30s command shows nothing, then everything.

**The answer never streams at all**, and is the worst case because it is the longest and most important text in the transcript. It is delivered as a tool call: the whole answer is buffered as the `content` argument, and the pane is only created on `ToolCallFinished`. In the screenshot the 17-joke answer appears as one raw-JSON dump inside a failed tool-call box.

## Design decisions

* **Stream tool-call arguments through the existing `ToolCallDelta` type.** `OllamaClient._parse_lines` emits `ToolCallDelta` as argument text arrives; `stream_step` forwards it as a new `ToolCallArgumentsDelta` bus event; the pane appends to a reactive. `ToolCallReady` still closes the call with the authoritative parsed mapping, which remains what gets executed and recorded — the streamed text is display only and is replaced by `format_arguments` output on ready, so a partial-JSON tail never lingers.
* **Ollama's chunking is the constraint, not the design.** Ollama's `/api/chat` may deliver a tool call in one chunk with fully-formed `arguments`. When it does, emitting a single `ToolCallDelta` carrying the whole serialized argument text before `ToolCallReady` is correct and sufficient — the pane's rendering path is then identical whether the model chunked or not, which is the consistency being asked for. Do not fabricate artificial chunking or add a typewriter delay; streaming means "render what has arrived as it arrives", not "animate".
* **Stream the answer as prose, not as JSON.** The `answer` pane is created on `ToolCallStarted` (as a real `AnswerPane`, not a `ToolCallPane`) and streams its `content` argument; verification and citation errors then update that same pane. This subsumes the answer-pane bug in `0030` and must be sequenced with it — `0030` owns the pane state machine, this milestone owns the delta transport feeding it.
* **Stream shell output incrementally.** `Shell.run` moves from `subprocess.run` to `Popen` with incremental reads so a long-running command fills its box as it goes. Keep the existing timeout semantics and the `(code, output)` return shape; publish a `ToolCallResultDelta` per read. This is the one item here that changes core execution rather than transport, so it is last and independently revertable.
* **No new pane kinds.** Every change above feeds an existing widget a reactive it already has or a sibling of one.

## [X] T001 Bus events for incremental tool call rendering

### Description

Add `ToolCallArgumentsDelta` and `ToolCallResultDelta` to `src/pico/core/events.py` (both carrying the pane `id` and a text chunk) and to the `BusEvent` union. Add matching `ToolCallPaneArgumentsDelta` / `ToolCallPaneResultDelta` messages in `src/pico/tui/messages.py` and translate them.

### Acceptance criteria

* `tests/core/test_events.py` and `tests/tui/test_messages.py` cover the new events and their translation.
* Fully annotated, passes strict Pyright.

## [X] T002 Tool call panes append argument and result deltas

### Description

Give `ToolCallPane` (`src/pico/tui/widgets.py`) reactives that accumulate streamed argument text and streamed result text, rendered in place of the current static `arguments_text` / `result_text` while the call is open. On `finish(...)`, the authoritative parsed values replace whatever was streamed.

### Acceptance criteria

* `tests/tui/test_widgets.py` covers: successive argument deltas accumulate in the rendered output; successive result deltas accumulate; `finish()` replaces streamed argument text with the formatted mapping and streamed result text with the final result; truncation (`RESULT_TRUNCATE_LENGTH`) still applies to the finished result.
* Fully annotated, passes strict Pyright.

## [X] T003 Ollama emits argument deltas

### Description

In `src/pico/llm/ollama.py`, emit a `ToolCallDelta` carrying the serialized argument text for each tool call in a chunk before yielding its `ToolCallReady`.

### Acceptance criteria

* `tests/llm/test_ollama.py` covers: a chunk containing a tool call yields a `ToolCallDelta` before the corresponding `ToolCallReady`; the `ToolCallReady` still carries the fully parsed `arguments` mapping unchanged; a stream with no tool calls yields no `ToolCallDelta`.
* Fully annotated, passes strict Pyright.

## [X] T004 The loop forwards argument deltas

### Description

Replace the `case ToolCallDelta(): pass` no-op in `stream_step` (`src/pico/core/loop.py`) with a publish of `ToolCallArgumentsDelta`. The pane id must be the runtime-minted id from `0027`, so the delta lands on the pane that `ToolCallStarted` will create — which requires minting the pane id when the call's deltas first appear, not in `tool_call_step`. Keep the mapping from a model `ToolCall` to its pane id on the runner so `tool_call_step` reuses it.

### Acceptance criteria

* `tests/core/test_loop.py` covers: argument deltas are published with the same pane id later used by that call's `ToolCallStarted` and `ToolCallFinished`; two concurrent tool calls in one turn keep their deltas on separate pane ids; a call whose deltas never arrive (model emits only `ToolCallReady`) still gets a pane id and renders.
* Fully annotated, passes strict Pyright.

## [X] T005 Shell output streams as it is produced

### Description

Rewrite `Shell.run` (`src/pico/core/actions.py`) to use `subprocess.Popen` with incremental output reads, invoking an optional callback per chunk, while preserving the `(exit_code, output)` return, the combined stdout/stderr capture, and the `timeout` behaviour (including killing the process group on timeout and still raising `ToolError`). Wire `tool_call_step` to publish `ToolCallResultDelta` from that callback.

### Acceptance criteria

* `tests/core/test_actions.py` covers: a command producing output over several reads invokes the callback more than once and returns the same combined output as before; a non-zero exit still returns its code; a command exceeding `timeout` still raises `ToolError` and does not leave a running child; stderr is still interleaved into the output.
* `tests/core/test_loop.py` covers: a shell tool call publishes `ToolCallResultDelta` events before its `ToolCallFinished`.
* Fully annotated, passes strict Pyright.

## [X] T006 Verify and finalize

### Description

Run `make check` and fix everything until green. Confirm `ToolCallDelta` now has both a producer and a consumer.

### Acceptance criteria

* `make check` passes, including the coverage floor.
* No `case ...: pass` no-op remains in `stream_step`.

### Completion

Commit:
