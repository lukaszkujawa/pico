import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import httpx

from pico.llm.budget import COMPLETION_RESERVE_CAP, completion_reserve
from pico.llm.client import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
    ToolResult,
    ToolSpec,
)

ANTHROPIC_VERSION = "2023-06-01"


def _lift_system(messages: list[Message]) -> tuple[str, list[Message]]:
    system = "\n\n".join(message.content for message in messages if message.role is Role.SYSTEM)
    rest = [message for message in messages if message.role is not Role.SYSTEM]
    return system, rest


def _tool_result_block(result: ToolResult) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": result.tool_call_id,
        "content": result.content,
        "is_error": result.is_error,
    }


def _assistant_payload(message: Message) -> dict[str, Any]:
    if not message.tool_calls:
        return {"role": "assistant", "content": message.content}
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    blocks += [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": dict(call.arguments)}
        for call in message.tool_calls
    ]
    return {"role": "assistant", "content": blocks}


def _messages_to_payload(messages: list[Message]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.TOOL:
            result = message.tool_result
            assert result is not None
            block = _tool_result_block(result)
            last = payload[-1] if payload else None
            if last is not None and last["role"] == "user" and isinstance(last["content"], list):
                cast(list[dict[str, Any]], last["content"]).append(block)
            else:
                payload.append({"role": "user", "content": [block]})
        elif message.role is Role.ASSISTANT:
            payload.append(_assistant_payload(message))
        else:
            payload.append({"role": "user", "content": message.content})
    return payload


def _tool_spec_to_payload(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.parameters,
    }


@dataclass
class _ToolUseBlock:
    call_id: str
    name: str
    arguments: str = ""


class AnthropicClient:
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        context_size: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport
        self._temperature = temperature
        self._max_tokens = (
            completion_reserve(context_size) if context_size else COMPLETION_RESERVE_CAP
        )

    def models(self) -> list[str]:
        try:
            with httpx.Client(transport=self._transport) as client:
                response = client.get(
                    f"{self._base_url}/models", headers=self._headers(), timeout=10.0
                )
                response.raise_for_status()
                listing: dict[str, Any] = response.json()
        except httpx.HTTPError as error:
            raise LLMError(str(error)) from error
        entries: list[dict[str, Any]] = listing.get("data") or []
        return sorted(model_id for entry in entries if (model_id := entry.get("id")))

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        system, rest = _lift_system(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": _messages_to_payload(rest),
            "stream": True,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [_tool_spec_to_payload(tool) for tool in tools]

        try:
            with (
                httpx.Client(transport=self._transport) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/messages",
                    json=payload,
                    headers=self._headers(),
                    timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
                ) as response,
            ):
                response.raise_for_status()
                yield from self._parse_lines(response.iter_lines())
        except httpx.HTTPError as error:
            raise LLMError(str(error)) from error

    def _parse_lines(self, lines: Iterator[str]) -> Iterator[StreamEvent]:
        blocks: dict[int, _ToolUseBlock] = {}
        finish_reason = "stop"
        input_tokens: int | None = None
        output_tokens: int | None = None

        for line in lines:
            if not line.startswith("data:"):
                continue
            chunk: dict[str, Any] = json.loads(line.removeprefix("data:").strip())

            match chunk.get("type"):
                case "message_start":
                    usage: dict[str, Any] = chunk["message"].get("usage") or {}
                    input_tokens = usage.get("input_tokens")
                case "content_block_start":
                    opened: dict[str, Any] = chunk["content_block"]
                    if opened["type"] == "tool_use":
                        block = _ToolUseBlock(call_id=opened["id"], name=opened["name"])
                        blocks[chunk["index"]] = block
                        yield ToolCallDelta(id=block.call_id, name=block.name, arguments_delta="")
                case "content_block_delta":
                    delta: dict[str, Any] = chunk["delta"]
                    match delta["type"]:
                        case "text_delta":
                            yield TextDelta(text=delta["text"])
                        case "thinking_delta":
                            yield ThinkingDelta(text=delta["thinking"])
                        case "input_json_delta":
                            block = blocks[chunk["index"]]
                            fragment: str = delta["partial_json"]
                            block.arguments += fragment
                            yield ToolCallDelta(
                                id=block.call_id, name=block.name, arguments_delta=fragment
                            )
                        case _:
                            pass
                case "content_block_stop":
                    closed = blocks.pop(chunk["index"], None)
                    if closed is not None:
                        yield self._finish_block(closed)
                case "message_delta":
                    finish_reason = chunk["delta"].get("stop_reason") or finish_reason
                    final_usage: dict[str, Any] = chunk.get("usage") or {}
                    output_tokens = final_usage.get("output_tokens", output_tokens)
                case "error":
                    raise LLMError(str(chunk.get("error")))
                case _:
                    pass

        yield GenerationComplete(
            finish_reason=finish_reason,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )

    def _finish_block(self, block: _ToolUseBlock) -> ToolCallReady:
        raw = block.arguments or "{}"
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LLMError(f"malformed arguments for tool call {block.name!r}: {raw!r}") from error
        if not isinstance(parsed, dict):
            raise LLMError(f"malformed arguments for tool call {block.name!r}: {raw!r}")
        arguments = cast(dict[str, Any], parsed)
        return ToolCallReady(
            tool_call=ToolCall(id=block.call_id, name=block.name, arguments=arguments)
        )
