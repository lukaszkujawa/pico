import json

import httpx
import pytest

from pico.llm.anthropic import ANTHROPIC_VERSION, AnthropicClient
from pico.llm.errors import LLMError
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


def _client(
    handler: httpx.MockTransport | None = None,
    api_key: str | None = None,
    context_size: int | None = None,
) -> AnthropicClient:
    return AnthropicClient(
        model="claude-test",
        base_url=BASE_URL,
        api_key=api_key,
        transport=handler,
        context_size=context_size,
    )


def _sse_response(events: list[dict[str, object]]) -> httpx.Response:
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(200, content=body)


def _message_start(input_tokens: int = 10) -> dict[str, object]:
    return {"type": "message_start", "message": {"usage": {"input_tokens": input_tokens}}}


def _message_end(stop_reason: str = "end_turn", output_tokens: int = 5) -> list[dict[str, object]]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]


def _tool_use_events(
    call_id: str, name: str, fragments: list[str], index: int = 0
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        }
    ]
    deltas: list[dict[str, object]] = [
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": fragment},
        }
        for fragment in fragments
    ]
    events += deltas
    events.append({"type": "content_block_stop", "index": index})
    return events


def test_stream_text_with_split_usage() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _message_start(input_tokens=10),
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": " world"},
                },
                {"type": "content_block_stop", "index": 0},
                *_message_end(stop_reason="end_turn", output_tokens=5),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events == [
        TextDelta(text="Hello"),
        TextDelta(text=" world"),
        GenerationComplete(finish_reason="end_turn", prompt_tokens=10, completion_tokens=5),
    ]


def test_stream_thinking_yields_thinking_delta() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _message_start(),
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "pondering"},
                },
                {"type": "content_block_stop", "index": 0},
                *_message_end(),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert events[0] == ThinkingDelta(text="pondering")


def test_stream_tool_arguments_accumulate_across_fragments() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _message_start(),
                *_tool_use_events("toolu_1", "search", ['{"que', 'ry": "pico"}']),
                *_message_end(stop_reason="tool_use"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="search pico")], []))

    assert events == [
        ToolCallDelta(id="toolu_1", name="search", arguments_delta=""),
        ToolCallDelta(id="toolu_1", name="search", arguments_delta='{"que'),
        ToolCallDelta(id="toolu_1", name="search", arguments_delta='ry": "pico"}'),
        ToolCallReady(tool_call=ToolCall(id="toolu_1", name="search", arguments={"query": "pico"})),
        GenerationComplete(finish_reason="tool_use", prompt_tokens=10, completion_tokens=5),
    ]


def test_stream_keeps_multiple_tool_use_blocks_apart() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _message_start(),
                *_tool_use_events("toolu_a", "search", ['{"q": "y"}'], index=0),
                *_tool_use_events("toolu_b", "fetch", ['{"url": "x"}'], index=1),
                *_message_end(stop_reason="tool_use"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    ready = [event.tool_call for event in events if isinstance(event, ToolCallReady)]
    assert ready == [
        ToolCall(id="toolu_a", name="search", arguments={"q": "y"}),
        ToolCall(id="toolu_b", name="fetch", arguments={"url": "x"}),
    ]


def test_stream_empty_arguments_parse_as_empty_mapping() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _message_start(),
                *_tool_use_events("toolu_1", "ping", []),
                *_message_end(stop_reason="tool_use"),
            ]
        )

    client = _client(httpx.MockTransport(handle))

    events = list(client.stream([Message(role=Role.USER, content="hi")], []))

    ready = events[1]
    assert isinstance(ready, ToolCallReady)
    assert ready.tool_call == ToolCall(id="toolu_1", name="ping", arguments={})


@pytest.mark.parametrize("fragments", [['{"query": '], ["[1, 2]"]])
def test_stream_malformed_tool_arguments_raise_llm_error(fragments: list[str]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [
                _message_start(),
                *_tool_use_events("toolu_1", "search", fragments),
                *_message_end(stop_reason="tool_use"),
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


def test_stream_error_event_raises_llm_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            [{"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}}]
        )

    client = _client(httpx.MockTransport(handle))

    with pytest.raises(LLMError, match="overloaded"):
        list(client.stream([Message(role=Role.USER, content="hi")], []))


def test_stream_request_payload_and_message_mapping() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_message_start(), *_message_end()])

    client = _client(httpx.MockTransport(handle), context_size=1000)
    messages = [
        Message(role=Role.SYSTEM, content="be brief"),
        Message(role=Role.USER, content="search pico"),
        Message(
            role=Role.ASSISTANT,
            content="looking",
            tool_calls=(
                ToolCall(id="toolu_1", name="search", arguments={"query": "pico"}),
                ToolCall(id="toolu_2", name="fetch", arguments={"url": "x"}),
            ),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(tool_call_id="toolu_1", content="found it", name="search"),
        ),
        Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id="toolu_2", content="boom", is_error=True, name="fetch"
            ),
        ),
    ]
    tools = [ToolSpec(name="search", description="search the web", parameters={"type": "object"})]

    list(client.stream(messages, tools))

    payload = json.loads(captured[0].content)
    assert captured[0].url.path == "/v1/messages"
    assert payload["model"] == "claude-test"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 250
    assert payload["system"] == "be brief"
    assert payload["messages"] == [
        {"role": "user", "content": "search pico"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "pico"}},
                {"type": "tool_use", "id": "toolu_2", "name": "fetch", "input": {"url": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "found it",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": "boom",
                    "is_error": True,
                },
            ],
        },
    ]
    assert payload["tools"] == [
        {
            "name": "search",
            "description": "search the web",
            "input_schema": {"type": "object"},
        }
    ]


def test_max_tokens_defaults_to_reserve_cap_without_context_size() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_message_start(), *_message_end()])

    client = _client(httpx.MockTransport(handle))

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert json.loads(captured[0].content)["max_tokens"] == 4096


def test_stream_sends_anthropic_headers() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_message_start(), *_message_end()])

    client = _client(httpx.MockTransport(handle), api_key="secret")

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert captured[0].headers["x-api-key"] == "secret"
    assert captured[0].headers["anthropic-version"] == ANTHROPIC_VERSION
    assert "authorization" not in captured[0].headers


def test_stream_omits_api_key_header_when_unset() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _sse_response([_message_start(), *_message_end()])

    client = _client(httpx.MockTransport(handle))

    list(client.stream([Message(role=Role.USER, content="hi")], []))

    assert "x-api-key" not in captured[0].headers


def test_models_returns_sorted_ids() -> None:
    captured: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-large"}, {"id": "claude-base"}]},
        )

    client = _client(httpx.MockTransport(handle))

    assert client.models() == ["claude-base", "claude-large"]
    assert captured[0].url.path == "/v1/models"
    assert captured[0].headers["anthropic-version"] == ANTHROPIC_VERSION


def test_models_connection_failure_raises_llm_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _client(httpx.MockTransport(handle))

    with pytest.raises(LLMError, match="refused"):
        client.models()
