import itertools
import json
from collections.abc import Iterator
from typing import Any

import httpx

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
    ToolSpec,
)


def _message_to_payload(message: Message) -> dict[str, Any]:
    if message.role is Role.TOOL:
        result = message.tool_result
        assert result is not None
        payload = {"role": Role.TOOL.value, "content": result.content}
        if result.name:
            payload["tool_name"] = result.name
        return payload

    payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "function": {"name": call.name, "arguments": dict(call.arguments)},
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


class OllamaClient:
    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
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
                    f"{self._base_url}/api/tags", headers=self._headers(), timeout=10.0
                )
                response.raise_for_status()
                listing: dict[str, Any] = response.json()
        except httpx.HTTPError as error:
            raise LLMError(str(error)) from error
        entries: list[dict[str, Any]] = listing.get("models") or []
        return [name for entry in entries if (name := entry.get("model"))]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [_message_to_payload(message) for message in messages],
            "stream": True,
        }
        if self._context_size is not None:
            payload["options"] = {"num_ctx": self._context_size}
        if tools:
            payload["tools"] = [_tool_spec_to_payload(tool) for tool in tools]

        try:
            with (
                httpx.Client(transport=self._transport) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/api/chat",
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
        for line in lines:
            if not line.strip():
                continue
            chunk: dict[str, Any] = json.loads(line)

            message: dict[str, Any] | None = chunk.get("message")
            if message:
                thinking: str = message.get("thinking", "")
                if thinking:
                    yield ThinkingDelta(text=thinking)

                content: str = message.get("content", "")
                if content:
                    yield TextDelta(text=content)

                raw_calls: list[dict[str, Any]] = message.get("tool_calls") or []
                for raw_call in raw_calls:
                    function: dict[str, Any] = raw_call["function"]
                    call_id: str = raw_call.get("id") or str(next(self._fallback_call_ids))
                    name: str = function["name"]
                    arguments: dict[str, Any] = function.get("arguments", {})
                    yield ToolCallDelta(
                        id=call_id,
                        name=name,
                        arguments_delta=json.dumps(arguments, separators=(", ", ": ")),
                    )
                    yield ToolCallReady(
                        tool_call=ToolCall(id=call_id, name=name, arguments=arguments)
                    )

            if chunk.get("done"):
                yield GenerationComplete(
                    finish_reason=chunk.get("done_reason", "stop"),
                    prompt_tokens=chunk.get("prompt_eval_count"),
                    completion_tokens=chunk.get("eval_count"),
                )
