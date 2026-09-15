import pytest

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


def test_role_values() -> None:
    assert Role.SYSTEM.value == "system"
    assert Role.USER.value == "user"
    assert Role.ASSISTANT.value == "assistant"
    assert Role.TOOL.value == "tool"


def test_tool_call_construction_and_equality() -> None:
    a = ToolCall(id="1", name="search", arguments={"query": "pico"})
    b = ToolCall(id="1", name="search", arguments={"query": "pico"})
    assert a == b
    with pytest.raises(AttributeError):
        a.id = "2"  # type: ignore[misc]


def test_tool_result_construction_and_equality() -> None:
    a = ToolResult(tool_call_id="1", content="ok")
    b = ToolResult(tool_call_id="1", content="ok", is_error=False)
    assert a == b
    assert a.is_error is False


def test_tool_result_error_flag() -> None:
    result = ToolResult(tool_call_id="1", content="boom", is_error=True)
    assert result.is_error is True


def test_message_defaults() -> None:
    message = Message(role=Role.USER, content="hello")
    assert message.tool_calls == ()
    assert message.tool_result is None


def test_message_with_tool_calls() -> None:
    call = ToolCall(id="1", name="search", arguments={})
    message = Message(role=Role.ASSISTANT, tool_calls=(call,))
    assert message.tool_calls == (call,)


def test_message_with_tool_result() -> None:
    result = ToolResult(tool_call_id="1", content="ok")
    message = Message(role=Role.TOOL, tool_result=result)
    assert message.tool_result == result


def test_message_equality() -> None:
    a = Message(role=Role.USER, content="hi")
    b = Message(role=Role.USER, content="hi")
    assert a == b


def test_tool_spec_construction_and_equality() -> None:
    a = ToolSpec(name="search", description="search the web", parameters={"type": "object"})
    b = ToolSpec(name="search", description="search the web", parameters={"type": "object"})
    assert a == b


def test_text_delta() -> None:
    delta = TextDelta(text="hello")
    assert delta.text == "hello"


def test_thinking_delta() -> None:
    delta = ThinkingDelta(text="pondering")
    assert delta.text == "pondering"


def test_tool_call_delta() -> None:
    delta = ToolCallDelta(id="1", name="search", arguments_delta='{"query":')
    assert delta.id == "1"
    assert delta.name == "search"
    assert delta.arguments_delta == '{"query":'


def test_tool_call_ready() -> None:
    call = ToolCall(id="1", name="search", arguments={"query": "pico"})
    ready = ToolCallReady(tool_call=call)
    assert ready.tool_call == call


def test_generation_complete_defaults() -> None:
    complete = GenerationComplete(finish_reason="stop")
    assert complete.prompt_tokens is None
    assert complete.completion_tokens is None


def test_generation_complete_with_usage() -> None:
    complete = GenerationComplete(finish_reason="stop", prompt_tokens=10, completion_tokens=5)
    assert complete.prompt_tokens == 10
    assert complete.completion_tokens == 5


def test_stream_event_exhaustive_match() -> None:
    def describe(
        event: TextDelta | ThinkingDelta | ToolCallDelta | ToolCallReady | GenerationComplete,
    ) -> str:
        match event:
            case TextDelta():
                return "text"
            case ThinkingDelta():
                return "thinking"
            case ToolCallDelta():
                return "tool_call_delta"
            case ToolCallReady():
                return "tool_call_ready"
            case GenerationComplete():
                return "complete"

    assert describe(TextDelta(text="hi")) == "text"
    assert describe(ThinkingDelta(text="hmm")) == "thinking"
    assert describe(ToolCallDelta(id="1", name="x", arguments_delta="")) == "tool_call_delta"
    assert (
        describe(ToolCallReady(tool_call=ToolCall(id="1", name="x", arguments={})))
        == "tool_call_ready"
    )
    assert describe(GenerationComplete(finish_reason="stop")) == "complete"
