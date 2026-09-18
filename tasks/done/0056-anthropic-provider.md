# Anthropic Provider

Complete frontier coverage with a Claude client behind the same `LLMClient` protocol. Anthropic's wire format differs from OpenAI's more than Ollama's does, so the mapping carries most of the work.

## Design decisions

* **New module** `src/pico/llm/anthropic.py` with `AnthropicClient`, constructor and methods matching the other clients, `httpx` with an injectable `transport`. No SDK.
* **Vendor dispatch**: add `anthropic` to the mapping introduced in milestone 0055.
* **Headers**: `x-api-key` and `anthropic-version`, not `Authorization: Bearer`. The version string is a module constant.
* **System messages are not a role.** `Role.SYSTEM` messages are lifted out of the list into the top-level `system` parameter; the remainder must alternate user and assistant. `SYSTEM_PROMPT` is always first, so this lift is unconditional.
* **Tool results are user-role content blocks**, not a `tool` role: `{"type": "tool_result", "tool_use_id": ..., "content": ..., "is_error": ...}`. `ToolResult.is_error` maps directly, which no other provider consumes.
* **Tool specs** use `input_schema`, not `function.parameters`.
* **max_tokens is required** on every request. Derive it from the completion reserve the context layer already computes, so the two agree rather than hard-coding a number.
* **Streaming** is typed SSE events, not delta objects: `content_block_start` opens a block (`text`, `thinking`, or `tool_use`), `content_block_delta` carries `text_delta`, `thinking_delta`, or `input_json_delta`, and `content_block_stop` closes it. `tool_use` id and name arrive on the start event while arguments accumulate as JSON fragments across deltas — emit `ToolCallReady` at the block's stop.
* **Thinking blocks** map to `ThinkingDelta`, the protocol's existing channel.
* **Usage** is split: `message_start` carries `input_tokens`, `message_delta` carries `output_tokens`. Both are held and emitted together on the final `GenerationComplete`.
* **models()** reads `GET /v1/models`, returning sorted ids.

## [X] T001 Anthropic client

### Description

Implement `AnthropicClient`: system-message lift, tool-result content blocks, `input_schema` tool specs, `max_tokens`, typed SSE parsing with block accumulation, split usage capture, and `LLMError` on HTTP and parse failures.

### Acceptance criteria

* System messages are lifted to the `system` parameter and absent from `messages`.
* Tool results serialise as user-role `tool_result` blocks preserving `tool_use_id` and `is_error`.
* `stream` yields `TextDelta`, `ThinkingDelta`, `ToolCallDelta`, `ToolCallReady`, and a final `GenerationComplete` with both token counts.
* Tool arguments split across `input_json_delta` fragments accumulate into one correctly parsed `ToolCallReady`; several tool-use blocks in one response stay distinct.
* Unparseable tool arguments and HTTP errors raise `LLMError`.
* Tests drive an injected `httpx` transport over recorded SSE event sequences, with no network access.
* `make check` passes.

## [X] T002 Register the vendor

### Description

Add `anthropic` to the vendor mapping in `app.py`.

### Acceptance criteria

* `LLM_VENDOR=anthropic` builds an `AnthropicClient`; existing vendors are unaffected.
* `make check` passes.
