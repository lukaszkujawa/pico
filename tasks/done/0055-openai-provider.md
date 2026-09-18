# OpenAI Provider

`build_llm_client` rejects every vendor but `ollama`, so Pico cannot run on a frontier model. Add an OpenAI-compatible client behind the existing `LLMClient` protocol.

## Design decisions

* **New module** `src/pico/llm/openai.py` with `OpenAIClient`, matching `OllamaClient`'s shape: same constructor arguments, `stream` and `models`, `httpx` with an injectable `transport` for tests. No vendor SDK — the Chat Completions wire format is stable and `httpx` is already a dependency.
* **Vendor dispatch**: `build_llm_client` in `app.py` becomes a vendor-to-constructor mapping; `UnsupportedVendorError` still covers unknown vendors. `LLM_BASE_URL` stays required, so OpenAI-compatible gateways work without new config.
* **Streaming**: SSE `data:` lines rather than Ollama's NDJSON, terminated by `data: [DONE]`. Deltas arrive under `choices[0].delta`.
* **Tool calls** stream as fragments: `delta.tool_calls[]` carries an `index`, an `id` and name on the first fragment only, and `function.arguments` as a JSON string split across chunks. The client accumulates per index and emits `ToolCallDelta` per fragment, then one `ToolCallReady` with parsed arguments when the choice finishes. This is the key difference from Ollama, which delivers arguments whole.
* **Malformed tool arguments** — unparseable accumulated JSON — raise `LLMError` rather than yielding a broken `ToolCall`.
* **Usage**: request `stream_options={"include_usage": True}` and map `usage.prompt_tokens` / `usage.completion_tokens` onto `GenerationComplete`, which `ContextMeter` depends on.
* **Reasoning text**, when a model returns it, maps to `ThinkingDelta`; absent, nothing is emitted.
* `context_size` is a Pico-side packing budget here, not a request parameter — unlike Ollama's `num_ctx`, it is not sent.
* `models()` reads `GET /v1/models` and returns sorted ids.

## [X] T001 OpenAI client

### Description

Implement `OpenAIClient` in `src/pico/llm/openai.py`: message and tool-spec payload mapping, SSE parsing, fragmented tool-call accumulation, usage capture, and `LLMError` on HTTP and parse failures.

### Acceptance criteria

* `stream` yields `TextDelta`, `ToolCallDelta`, `ToolCallReady`, and a final `GenerationComplete` carrying prompt and completion tokens.
* Tool-call arguments split across several chunks accumulate into one `ToolCallReady` with correctly parsed arguments; multiple concurrent tool calls are kept apart by index.
* Unparseable tool arguments and HTTP errors raise `LLMError`.
* `models()` returns the available model ids.
* Tests mirror `tests/llm/test_ollama.py`, driving an injected `httpx` transport with no network access.
* `make check` passes.

## [X] T002 Vendor dispatch

### Description

Turn `build_llm_client` into a vendor mapping covering `ollama` and `openai`, keeping `UnsupportedVendorError` for anything else.

### Acceptance criteria

* `LLM_VENDOR=openai` builds an `OpenAIClient`; `ollama` is unchanged; an unknown vendor still raises `UnsupportedVendorError`.
* TUI, headless, and eval entry points need no changes beyond the shared builder.
* `make check` passes.
