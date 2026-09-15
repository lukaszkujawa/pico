# LLM Abstraction

Build the vendor-agnostic LLM layer in `src/pico/llm/`. Application core and TUI must never depend on a specific vendor, only on the types and protocol defined here. All generation is streaming only; there is no non-streaming call path.

## [ ] T001 Core message and content types

### Description

In `src/pico/llm/types.py`, define the vendor-agnostic conversation types:

* `Role` — enum: `system`, `user`, `assistant`, `tool`.
* `ToolCall` — id, tool name, arguments (JSON-compatible mapping).
* `ToolResult` — tool call id, content, and an `is_error` flag.
* `Message` — role plus content; an assistant message may carry a list of `ToolCall`; a tool message carries a `ToolResult`.
* `ToolSpec` — name, description, JSON schema for parameters, used to advertise available tools to the model.

Use frozen dataclasses or `NamedTuple`s, whichever keeps the module simplest. No behaviour beyond data.

### Acceptance criteria

* Types are immutable value objects.
* All types are fully annotated and pass strict Pyright.
* `tests/llm/test_types.py` covers construction and equality of each type.

## [ ] T002 Streaming event types

### Description

In `src/pico/llm/types.py` (or `src/pico/llm/events.py` if that reads cleaner), define the events a streaming generation emits, in the order a consumer will see them:

* `TextDelta` — a chunk of assistant text.
* `ToolCallDelta` — a chunk of an in-progress tool call being assembled.
* `ToolCallReady` — a fully assembled `ToolCall`.
* `GenerationComplete` — end of stream, carrying the finish reason and token usage if the vendor reports it.

Model this as a single discriminated union type, e.g. `StreamEvent = TextDelta | ToolCallDelta | ToolCallReady | GenerationComplete`, so consumers can exhaustively match on it.

### Acceptance criteria

* `StreamEvent` is a closed union that Pyright can exhaustively narrow with `match`.
* `tests/llm/test_types.py` (or a new `test_events.py`) covers construction of each event variant.

## [ ] T003 LLMClient protocol

### Description

In `src/pico/llm/client.py`, define the `LLMClient` protocol (`typing.Protocol`) that every vendor implementation must satisfy:

* A single streaming method, e.g. `stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]`.
* Keep the surface minimal: this is the only entry point application core needs.

This is a structural protocol, not a base class — implementations do not inherit from it.

### Acceptance criteria

* `LLMClient` is a `Protocol` with one streaming method and no implementation.
* Fully annotated, passes strict Pyright.
* A test using a minimal fake client confirms the protocol shape is usable (e.g. a fake satisfies it structurally and can be passed where `LLMClient` is expected).

## [ ] T004 Ollama client implementation

### Description

In `src/pico/llm/ollama.py`, implement `OllamaClient` satisfying `LLMClient` against a local Ollama server's streaming chat endpoint (`/api/chat` with `"stream": true`, newline-delimited JSON).

* Add `httpx` as a runtime dependency with `uv add httpx`.
* Constructor takes the model name and base URL (default `http://localhost:11434`).
* Translate `Message`/`ToolSpec` into Ollama's request schema, and translate each streamed line back into the appropriate `StreamEvent`.
* Surface transport/HTTP failures as a dedicated `pico.llm.errors.LLMError` rather than leaking `httpx` exceptions.

### Acceptance criteria

* `OllamaClient` satisfies the `LLMClient` protocol.
* Tests in `tests/llm/test_ollama.py` mock the HTTP layer (no real network access, no dependency on a running Ollama server) and cover: plain text streaming, tool call streaming, and an HTTP error path raising `LLMError`.
* Fully annotated, passes strict Pyright.

## [ ] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Confirm `src/pico/llm/__init__.py` re-exports the public surface (`Message`, `Role`, `ToolCall`, `ToolResult`, `ToolSpec`, `StreamEvent` variants, `LLMClient`, `OllamaClient`, `LLMError`) needed by the rest of the codebase.

### Acceptance criteria

* `make check` passes with no errors.
* Nothing outside `src/pico/llm/` needs to import from `pico.llm.ollama` or `pico.llm.types` directly — the package `__init__.py` is the public surface.

### Completion

Commit:
