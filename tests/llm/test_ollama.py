import json

import httpx
import pytest

from pico.llm.errors import LLMError
from pico.llm.ollama import OllamaClient
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallReady,
    ToolSpec,
)


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


def test_stream_thinking_and_content_yields_both_in_order() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {
                    "message": {"role": "assistant", "thinking": "pondering", "content": "Hello"},
                    "done": False,
                },
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                },
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [
        ThinkingDelta(text="pondering"),
        TextDelta(text="Hello"),
        GenerationComplete(finish_reason="stop"),
    ]


def test_stream_only_thinking_yields_no_text_delta() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {"message": {"role": "assistant", "thinking": "pondering"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [ThinkingDelta(text="pondering"), GenerationComplete(finish_reason="stop")]


def test_stream_only_content_yields_no_thinking_delta() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {"message": {"role": "assistant", "content": "Hello"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True},
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [TextDelta(text="Hello"), GenerationComplete(finish_reason="stop")]


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

    assert len(events) == 3
    delta = events[0]
    assert isinstance(delta, ToolCallDelta)
    assert delta.id == "call_1"
    assert delta.name == "search"
    ready = events[1]
    assert isinstance(ready, ToolCallReady)
    assert ready.tool_call.name == "search"
    assert ready.tool_call.arguments == {"query": "pico"}
    assert events[2] == GenerationComplete(finish_reason="tool_calls")


def test_stream_uses_explicit_call_id_verbatim() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "call_abc", "function": {"name": "search", "arguments": {}}}
                        ],
                    },
                    "done": False,
                },
                {"message": {"content": ""}, "done": True, "done_reason": "tool_calls"},
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    ready = events[1]
    assert isinstance(ready, ToolCallReady)
    assert ready.tool_call.id == "call_abc"


def test_successive_streams_with_omitted_ids_do_not_repeat_fallback_ids() -> None:
    def two_calls_no_id(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "search", "arguments": {}}},
                            {"function": {"name": "search", "arguments": {}}},
                        ],
                    },
                    "done": False,
                },
                {"message": {"content": ""}, "done": True, "done_reason": "tool_calls"},
            ]
        )

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(two_calls_no_id))

    first_stream = [
        event.tool_call.id
        for event in client.stream([Message(role=Role.USER, content="hi")], [])
        if isinstance(event, ToolCallReady)
    ]
    second_stream = [
        event.tool_call.id
        for event in client.stream([Message(role=Role.USER, content="hi")], [])
        if isinstance(event, ToolCallReady)
    ]

    assert len(set(first_stream + second_stream)) == 4


def test_stream_sends_authorization_header_when_api_key_set() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ndjson_response([{"message": {"content": ""}, "done": True}])

    client = OllamaClient(model="qwen3", api_key="secret", transport=httpx.MockTransport(handle))

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert captured[0].headers["authorization"] == "Bearer secret"


def test_stream_omits_authorization_header_when_no_api_key() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ndjson_response([{"message": {"content": ""}, "done": True}])

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert "authorization" not in captured[0].headers


def test_stream_http_error_raises_llm_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content="internal error")

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))

    with pytest.raises(LLMError):
        list(client.stream([Message(role=Role.USER, content="hi")], []))


def test_context_size_is_sent_as_num_ctx() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ndjson_response([{"done": True, "done_reason": "stop"}])

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle), context_size=16000)
    list(client.stream([Message(role=Role.USER, content="hi")], []))

    payload = json.loads(captured[0].content)
    assert payload["options"] == {"num_ctx": 16000}


def test_without_context_size_no_options_are_sent() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ndjson_response([{"done": True, "done_reason": "stop"}])

    client = OllamaClient(model="qwen3", transport=httpx.MockTransport(handle))
    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert "options" not in json.loads(captured[0].content)
