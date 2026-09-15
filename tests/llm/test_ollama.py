import json

import httpx
import pytest

from pico.llm.errors import LLMError
from pico.llm.ollama import OllamaClient
from pico.llm.types import GenerationComplete, Message, Role, TextDelta, ToolCallReady, ToolSpec


def _ndjson_response(lines: list[dict[str, object]]) -> httpx.Response:
    body = "\n".join(json.dumps(line) for line in lines)
    return httpx.Response(200, content=body)


def test_stream_plain_text() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {"message": {"role": "assistant", "content": "Hello"}, "done": False},
                {"message": {"role": "assistant", "content": " world"}, "done": False},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 3,
                    "eval_count": 2,
                },
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [
        TextDelta(text="Hello"),
        TextDelta(text=" world"),
        GenerationComplete(finish_reason="stop", prompt_tokens=3, completion_tokens=2),
    ]


def test_stream_tool_call() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "search",
                                    "arguments": {"query": "pico"},
                                },
                            }
                        ],
                    },
                    "done": False,
                },
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "tool_calls",
                },
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    tools = [ToolSpec(name="search", description="search the web", parameters={"type": "object"})]
    events = list(client.stream([Message(role=Role.USER, content="search pico")], tools))

    assert len(events) == 2
    ready = events[0]
    assert isinstance(ready, ToolCallReady)
    assert ready.tool_call.name == "search"
    assert ready.tool_call.arguments == {"query": "pico"}
    assert events[1] == GenerationComplete(finish_reason="tool_calls")


def test_stream_http_error_raises_llm_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content="internal error")

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    with pytest.raises(LLMError):
        list(client.stream([Message(role=Role.USER, content="hi")], []))
