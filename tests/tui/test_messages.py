from pico.core.events import (
    AnswerSettled,
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
    ToolCallResultDelta,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.messages import (
    AnswerPaneCreate,
    AnswerPaneSettle,
    AssistantPaneClose,
    AssistantPaneCreate,
    AssistantPaneDelta,
    ErrorMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ThinkingPaneClose,
    ThinkingPaneCreate,
    ThinkingPaneDelta,
    ToolCallPaneArgumentsDelta,
    ToolCallPaneClose,
    ToolCallPaneCreate,
    ToolCallPaneResultDelta,
    format_arguments,
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
    message = translate(ToolCallStarted(id="1", name="search", arguments={"q": "pico"}))
    assert isinstance(message, ToolCallPaneCreate)
    assert message.pane_id == "1"
    assert message.name == "search"
    assert message.arguments == "pico"


def test_format_arguments_single_string_shows_bare_value() -> None:
    assert format_arguments({"command": "ls -la | head"}) == "ls -la | head"


def test_format_arguments_multiple_show_one_named_line_each() -> None:
    formatted = format_arguments({"path": "notes.txt", "content": "hello"})
    assert formatted == "path: notes.txt\ncontent: hello"


def test_format_arguments_non_string_values_render_as_json() -> None:
    formatted = format_arguments({"question": "who?", "fields": {"team": "string"}})
    assert formatted == 'question: who?\nfields: {"team": "string"}'


def test_format_arguments_empty_renders_empty() -> None:
    assert format_arguments({}) == ""


def test_translate_tool_call_arguments_delta() -> None:
    message = translate(ToolCallArgumentsDelta(id="1", name="search", text='{"q":'))
    assert isinstance(message, ToolCallPaneArgumentsDelta)
    assert message.pane_id == "1"
    assert message.name == "search"
    assert message.text == '{"q":'


def test_translate_tool_call_result_delta() -> None:
    message = translate(ToolCallResultDelta(id="1", text="line\n"))
    assert isinstance(message, ToolCallPaneResultDelta)
    assert message.pane_id == "1"
    assert message.text == "line\n"


def test_translate_tool_call_finished() -> None:
    tool_call = ToolCall(id="1", name="search", arguments={})
    message = translate(ToolCallFinished(id="1", tool_call=tool_call, result="ok", is_error=False))
    assert isinstance(message, ToolCallPaneClose)
    assert message.pane_id == "1"
    assert message.result == "ok"
    assert message.is_error is False


def test_translate_answer_call_started_becomes_answer_pane_create() -> None:
    message = translate(ToolCallStarted(id="1", name="answer", arguments={"content": "42"}))
    assert isinstance(message, AnswerPaneCreate)
    assert message.pane_id == "1"


def test_translate_accepted_answer_settled_becomes_answer_pane_settle() -> None:
    message = translate(
        AnswerSettled(id="1", content="42", accepted=True, reason=None, verify=None)
    )
    assert isinstance(message, AnswerPaneSettle)
    assert message.pane_id == "1"
    assert message.content == "42"
    assert message.accepted is True
    assert message.reason is None
    assert message.verify is None


def test_translate_rejected_answer_settled_becomes_answer_pane_settle() -> None:
    message = translate(
        AnswerSettled(id="1", content="", accepted=False, reason="unknown fact citation(s): [3]")
    )
    assert isinstance(message, AnswerPaneSettle)
    assert message.accepted is False
    assert message.reason == "unknown fact citation(s): [3]"


def test_translate_error_occurred() -> None:
    message = translate(ErrorOccurred(message="bad"))
    assert isinstance(message, ErrorMessage)
    assert message.message == "bad"
