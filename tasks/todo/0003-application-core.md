# Application Core

Build the application core in `src/pico/core/`: the message bus that decouples the agentic loop from any consumer (such as the TUI), and the agentic loop itself. Depends on the LLM abstraction from `0002-llm-abstraction.md`.

## [ ] T001 Application bus

### Description

In `src/pico/core/bus.py`, implement `Bus`, a simple in-process publish/subscribe message bus:

* `publish(event: BusEvent) -> None` — hands an event to all current subscribers.
* `subscribe(self) -> Iterator[BusEvent]` (or a queue-returning equivalent) — lets a consumer receive events without knowing who publishes them.
* Thread-safe: application core will run its loop on one thread and publish events; a consumer (the future TUI) will read from another thread. Use `queue.Queue` per subscriber rather than inventing new synchronization primitives.

Keep this generic over `BusEvent` — the bus itself does not know about thinking boxes or tool calls, only about moving events from publishers to subscribers.

### Acceptance criteria

* `Bus` has no knowledge of specific event payloads, only a `BusEvent` type it moves.
* `tests/core/test_bus.py` covers: a subscriber receives events published after subscribing, multiple subscribers each receive their own copy, and publishing from one thread is observed by a subscriber reading on another thread.
* Fully annotated, passes strict Pyright.

## [ ] T002 Agent run events

### Description

In `src/pico/core/events.py`, define the closed set of `BusEvent` variants the agentic loop publishes, covering the lifecycle a consumer needs to render a run:

* `RunStarted`, `RunFinished` (with a result/error outcome).
* `AssistantTextStarted`, `AssistantTextDelta`, `AssistantTextFinished` — the "thinking box" lifecycle: create it, stream tokens into it, close it.
* `ToolCallStarted`, `ToolCallArgumentsDelta`, `ToolCallFinished` — the "tool call box" lifecycle: create it, stream its arguments/output, close it with a result.
* `ErrorOccurred` — a recoverable or fatal error surfaced to the consumer.

Model `BusEvent` as a discriminated union of these, mirroring the `StreamEvent` union style from the LLM layer.

### Acceptance criteria

* `BusEvent` is a closed union Pyright can exhaustively narrow with `match`.
* `tests/core/test_events.py` covers construction of each variant.
* Fully annotated, passes strict Pyright.

## [ ] T003 Tool registry

### Description

In `src/pico/core/tools.py`, define a minimal `Tool` domain object (name, `ToolSpec` for the model, and a callable that executes it given parsed arguments) and a `ToolRegistry` that looks tools up by name and exposes their `ToolSpec` list for the LLM client.

No built-in tools are implemented yet — this milestone only needs the registry mechanics, proven with a trivial test-only tool (e.g. an echo tool).

### Acceptance criteria

* `ToolRegistry` can register a `Tool`, list `ToolSpec`s for all registered tools, and execute a tool by name given a `ToolCall`.
* Unknown tool names raise a dedicated `pico.core.errors.UnknownToolError`.
* `tests/core/test_tools.py` covers registration, spec listing, successful execution, and the unknown-tool error path.
* Fully annotated, passes strict Pyright.

## [ ] T004 Agentic loop

### Description

In `src/pico/core/agent.py`, implement `Run`, which drives one agent run to completion:

* Constructed with an `LLMClient`, a `ToolRegistry`, a `Bus`, and the initial conversation (`list[Message]`).
* Publishes `RunStarted`, then repeatedly: streams from the `LLMClient`, translating `StreamEvent`s into the matching bus events (`AssistantText*`, `ToolCall*`), executes any tool calls via the `ToolRegistry`, appends results to the conversation, and loops until the model produces a plain-text finish with no further tool calls.
* Publishes `RunFinished` on success or `ErrorOccurred` + `RunFinished` (error outcome) on failure. The loop must not raise out of `Run.execute()` for ordinary tool or LLM errors — those become `ErrorOccurred` events instead.

### Acceptance criteria

* `Run.execute()` drives a full loop: text-only response ends the run; a tool call is executed and its result fed back for another turn.
* `tests/core/test_agent.py` uses a fake `LLMClient` (satisfying the protocol from `0002-llm-abstraction.md`) and a real `ToolRegistry` with a test tool, and asserts the exact sequence of bus events for: a plain text run, a single tool-call round trip, and an LLM error surfaced as `ErrorOccurred`.
* Fully annotated, passes strict Pyright.

## [ ] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Confirm `src/pico/core/__init__.py` re-exports the public surface (`Bus`, `BusEvent` variants, `Tool`, `ToolRegistry`, `Run`, `UnknownToolError`) needed by the TUI and entry point.

### Acceptance criteria

* `make check` passes with no errors.
* Nothing outside `src/pico/core/` needs to import from `pico.core.bus`, `pico.core.events`, `pico.core.tools`, or `pico.core.agent` directly.

### Completion

Commit:
