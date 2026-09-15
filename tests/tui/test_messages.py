from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.messages import (
    AssistantPaneClose,
    AssistantPaneCreate,
    AssistantPaneDelta,
    ErrorMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ThinkingPaneClose,
    ThinkingPaneCreate,
    ThinkingPaneDelta,
    ToolCallPaneClose,
    ToolCallPaneCreate,
    ToolCallPaneDelta,
    translate,
)


def test_translate_run_started() -> None:
    message = translate(RunStarted())
    assert isinstance(message, RunStartedMessage)


def test_translate_run_finished() -> None:
    message = translate(RunFinished(error="boom"))
    assert isinstance(message, RunFinishedMessage)
    assert message.error == "boom"


def test_translate_assistant_text_started() -> None:
    message = translate(AssistantTextStarted(id="0"))
    assert isinstance(message, AssistantPaneCreate)
    assert message.pane_id == "0"


def test_translate_assistant_text_delta() -> None:
    message = translate(AssistantTextDelta(id="0", text="hi"))
    assert isinstance(message, AssistantPaneDelta)
    assert message.pane_id == "0"
    assert message.text == "hi"


def test_translate_assistant_text_finished() -> None:
    message = translate(AssistantTextFinished(id="0"))
    assert isinstance(message, AssistantPaneClose)
    assert message.pane_id == "0"


def test_translate_assistant_thinking_started() -> None:
    message = translate(AssistantThinkingStarted(id="0"))
    assert isinstance(message, ThinkingPaneCreate)
    assert message.pane_id == "0"


def test_translate_assistant_thinking_delta() -> None:
    message = translate(AssistantThinkingDelta(id="0", text="hmm"))
    assert isinstance(message, ThinkingPaneDelta)
    assert message.pane_id == "0"
    assert message.text == "hmm"


def test_translate_assistant_thinking_finished() -> None:
    message = translate(AssistantThinkingFinished(id="0"))
    assert isinstance(message, ThinkingPaneClose)
    assert message.pane_id == "0"


def test_translate_tool_call_started() -> None:
    message = translate(ToolCallStarted(id="1", name="search"))
    assert isinstance(message, ToolCallPaneCreate)
    assert message.pane_id == "1"
    assert message.name == "search"


def test_translate_tool_call_arguments_delta() -> None:
    message = translate(ToolCallArgumentsDelta(id="1", arguments_delta='{"q":'))
    assert isinstance(message, ToolCallPaneDelta)
    assert message.pane_id == "1"
    assert message.text == '{"q":'


def test_translate_tool_call_finished() -> None:
    tool_call = ToolCall(id="1", name="search", arguments={})
    message = translate(ToolCallFinished(id="1", tool_call=tool_call, result="ok", is_error=False))
    assert isinstance(message, ToolCallPaneClose)
    assert message.pane_id == "1"
    assert message.result == "ok"
    assert message.is_error is False


def test_translate_error_occurred() -> None:
    message = translate(ErrorOccurred(message="bad"))
    assert isinstance(message, ErrorMessage)
    assert message.message == "bad"
