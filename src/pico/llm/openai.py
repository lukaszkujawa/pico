import itertools
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import httpx

from pico.llm.errors import LLMError
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
    ToolSpec,
)


def _message_to_payload(message: Message) -> dict[str, Any]:
    if message.role is Role.TOOL:
        result = message.tool_result
        assert result is not None
        return {
            "role": Role.TOOL.value,
            "content": result.content,
            "tool_call_id": result.tool_call_id,
        }

    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
            }
            for call in message.tool_calls
        ]
    return payload


def _tool_spec_to_payload(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


@dataclass
class _PendingCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


class OpenAIClient:
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        context_size: int | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport
        self._context_size = context_size
        self._fallback_call_ids = itertools.count()

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
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_message_to_payload(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [_tool_spec_to_payload(tool) for tool in tools]

        try:
            with (
                httpx.Client(transport=self._transport) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
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
        pending: dict[int, _PendingCall] = {}
        finish_reason = "stop"
        prompt_tokens: int | None = None
        completion_tokens: int | None = None

        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            chunk: dict[str, Any] = json.loads(data)

            usage: dict[str, Any] | None = chunk.get("usage")
            if usage:
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")

            choices: list[dict[str, Any]] = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta: dict[str, Any] = choice.get("delta") or {}

            thinking: str = delta.get("reasoning") or delta.get("reasoning_content") or ""
            if thinking:
                yield ThinkingDelta(text=thinking)

            content: str = delta.get("content") or ""
            if content:
                yield TextDelta(text=content)

            fragments: list[dict[str, Any]] = delta.get("tool_calls") or []
            for fragment in fragments:
                yield self._accumulate(pending, fragment)

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
                for index in sorted(pending):
                    yield self._finish_call(pending[index])
                pending.clear()

        yield GenerationComplete(
            finish_reason=finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _accumulate(
        self, pending: dict[int, _PendingCall], fragment: dict[str, Any]
    ) -> ToolCallDelta:
        index: int = fragment.get("index", 0)
        call = pending.setdefault(index, _PendingCall())
        if fragment.get("id"):
            call.id = fragment["id"]
        elif not call.id:
            call.id = str(next(self._fallback_call_ids))
        function: dict[str, Any] = fragment.get("function") or {}
        if function.get("name"):
            call.name = function["name"]
        arguments_delta: str = function.get("arguments") or ""
        call.arguments += arguments_delta
        return ToolCallDelta(id=call.id, name=call.name, arguments_delta=arguments_delta)

    def _finish_call(self, call: _PendingCall) -> ToolCallReady:
        raw = call.arguments or "{}"
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LLMError(f"malformed arguments for tool call {call.name!r}: {raw!r}") from error
        if not isinstance(parsed, dict):
            raise LLMError(f"malformed arguments for tool call {call.name!r}: {raw!r}")
        arguments = cast(dict[str, Any], parsed)
        return ToolCallReady(tool_call=ToolCall(id=call.id, name=call.name, arguments=arguments))
