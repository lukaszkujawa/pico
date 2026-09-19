import json

import httpx
import pytest

from pico.llm.client import LLMError
from pico.llm.openai import OpenAIClient
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
    ToolResult,
    ToolSpec,
)

BASE_URL = "http://localhost:8000/v1"


def _client(handler: httpx.MockTransport | None = None, api_key: str | None = None) -> OpenAIClient:
    return OpenAIClient(model="gpt-test", base_url=BASE_URL, api_key=api_key, transport=handler)


def _sse_response(chunks: list[dict[str, object]]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(200, content=body + "data: [DONE]\n\n")


def _delta_chunk(delta: dict[str, object], finish_reason: str | None = None) -> dict[str, object]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def test_stream_plain_text_with_usage() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk({"role": "assistant", "content": "Hello"}),
                _delta_chunk({"content": " world"}),
                _delta_chunk({}, finish_reason="stop"),
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [
        TextDelta(text="Hello"),
        TextDelta(text=" world"),
        GenerationComplete(finish_reason="stop", prompt_tokens=3, completion_tokens=2),
    ]


@pytest.mark.parametrize("key", ["reasoning", "reasoning_content"])
def test_stream_reasoning_yields_thinking_delta(key: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk({key: "pondering"}),
                _delta_chunk({"content": "Hello"}),
                _delta_chunk({}, finish_reason="stop"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [
        ThinkingDelta(text="pondering"),
        TextDelta(text="Hello"),
        GenerationComplete(finish_reason="stop"),
    ]


def test_stream_tool_call_arguments_accumulate_across_chunks() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "search", "arguments": '{"que'},
                            }
                        ]
                    }
                ),
                _delta_chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "pico"}'}}]}
                ),
                _delta_chunk({}, finish_reason="tool_calls"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    tools = [ToolSpec(name="search", description="search the web", parameters={"type": "object"})]
    events = list(client.stream([Message(role=Role.USER, content="search pico")], tools))

    assert events == [
        ToolCallDelta(id="call_1", name="search", arguments_delta='{"que'),
        ToolCallDelta(id="call_1", name="search", arguments_delta='ry": "pico"}'),
        ToolCallReady(tool_call=ToolCall(id="call_1", name="search", arguments={"query": "pico"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def test_stream_keeps_concurrent_tool_calls_apart_by_index() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "search", "arguments": '{"q":'},
                            },
                            {
                                "index": 1,
                                "id": "call_b",
                                "function": {"name": "fetch", "arguments": '{"url":'},
                            },
                        ]
                    }
                ),
                _delta_chunk(
                    {
                        "tool_calls": [
                            {"index": 1, "function": {"arguments": ' "x"}'}},
                            {"index": 0, "function": {"arguments": ' "y"}'}},
                        ]
                    }
                ),
                _delta_chunk({}, finish_reason="tool_calls"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    ready = [event.tool_call for event in events if isinstance(event, ToolCallReady)]
    assert ready == [
        ToolCall(id="call_a", name="search", arguments={"q": "y"}),
        ToolCall(id="call_b", name="fetch", arguments={"url": "x"}),
    ]


def test_stream_empty_arguments_parse_as_empty_mapping() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(
                    {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "ping"}}]}
                ),
                _delta_chunk({}, finish_reason="tool_calls"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    ready = events[1]
    assert isinstance(ready, ToolCallReady)
    assert ready.tool_call == ToolCall(id="call_1", name="ping", arguments={})


def test_stream_assigns_fallback_ids_when_omitted() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "search", "arguments": "{}"}},
                            {"index": 1, "function": {"name": "search", "arguments": "{}"}},
                        ]
                    }
                ),
                _delta_chunk({}, finish_reason="tool_calls"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    ids = {
        event.tool_call.id
        for event in client.stream([Message(role=Role.USER, content="hi")], [])
        if isinstance(event, ToolCallReady)
    } | {
        event.tool_call.id
        for event in client.stream([Message(role=Role.USER, content="hi")], [])
        if isinstance(event, ToolCallReady)
    }

    assert len(ids) == 4


@pytest.mark.parametrize("raw", ['{"query": ', "[1, 2]"])
def test_stream_malformed_tool_arguments_raise_llm_error(raw: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _delta_chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "search", "arguments": raw},
                            }
                        ]
                    }
                ),
                _delta_chunk({}, finish_reason="tool_calls"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    with pytest.raises(LLMError, match="search"):
        list(client.stream([Message(role=Role.USER, content="hi")], []))


def test_stream_http_error_raises_llm_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content="internal error")

    client = _client(httpx.MockTransport(handle))

    with pytest.raises(LLMError):
        list(client.stream([Message(role=Role.USER, content="hi")], []))


def test_stream_request_payload_and_message_mapping() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_delta_chunk({}, finish_reason="stop")])

    client = _client(httpx.MockTransport(handle))
    messages = [
        Message(role=Role.USER, content="search pico"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=(ToolCall(id="call_1", name="search", arguments={"query": "pico"}),),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="call_1", content="found it", name="search"),
        ),
    ]
    tools = [ToolSpec(name="search", description="search the web", parameters={"type": "object"})]

    list(client.stream(messages, tools))

    payload = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/chat/completions"
    assert payload["model"] == "gpt-test"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert "options" not in payload
    assert payload["messages"] == [
        {"role": "user", "content": "search pico"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query": "pico"}'},
                }
            ],
        },
        {"role": "tool", "content": "found it", "tool_call_id": "call_1"},
    ]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "search the web",
                "parameters": {"type": "object"},
            },
        }
    ]


def test_stream_sends_authorization_header_when_api_key_set() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_delta_chunk({}, finish_reason="stop")])

    client = _client(httpx.MockTransport(handle), api_key="secret")

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert captured[0].headers["authorization"] == "Bearer secret"


def test_stream_omits_authorization_header_when_no_api_key() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_delta_chunk({}, finish_reason="stop")])

    client = _client(httpx.MockTransport(handle))

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert "authorization" not in captured[0].headers


def test_models_returns_sorted_ids() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-large"}, {"id": "gpt-base"}]},
        )

    client = _client(httpx.MockTransport(handle))

    assert client.models() == ["gpt-base", "gpt-large"]
    assert captured[0].url.path == "/v1/models"


def test_models_sends_the_api_key_when_configured() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": []})

    client = _client(httpx.MockTransport(handle), api_key="secret")

    assert client.models() == []
    assert captured[0].headers["authorization"] == "Bearer secret"


def test_models_connection_failure_raises_llm_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _client(httpx.MockTransport(handle))

    with pytest.raises(LLMError, match="refused"):
        client.models()
