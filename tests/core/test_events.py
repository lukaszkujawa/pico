from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BusEvent,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall


def test_run_started() -> None:
    assert RunStarted() == RunStarted()


def test_run_finished_defaults() -> None:
    event = RunFinished()
    assert event.error is None


def test_run_finished_with_error() -> None:
    event = RunFinished(error="boom")
    assert event.error == "boom"


def test_assistant_text_started() -> None:
    event = AssistantTextStarted(id="1")
    assert event.id == "1"


def test_assistant_text_delta() -> None:
    event = AssistantTextDelta(id="1", text="hi")
    assert event.id == "1"
    assert event.text == "hi"


def test_assistant_text_finished() -> None:
    event = AssistantTextFinished(id="1")
    assert event.id == "1"


def test_assistant_thinking_started() -> None:
    event = AssistantThinkingStarted(id="1")
    assert event.id == "1"


def test_assistant_thinking_delta() -> None:
    event = AssistantThinkingDelta(id="1", text="hmm")
    assert event.id == "1"
    assert event.text == "hmm"


def test_assistant_thinking_finished() -> None:
    event = AssistantThinkingFinished(id="1")
    assert event.id == "1"


def test_tool_call_started() -> None:
    event = ToolCallStarted(id="1", name="echo", arguments={"x": 1})
    assert event.id == "1"
    assert event.name == "echo"
    assert event.arguments == {"x": 1}


def test_tool_call_finished() -> None:
    call = ToolCall(id="1", name="echo", arguments={})
    event = ToolCallFinished(id="1", tool_call=call, result="ok")
    assert event.tool_call == call
    assert event.result == "ok"
    assert event.is_error is False


def test_generation_completed_defaults_to_unknown_token_counts() -> None:
    event = GenerationCompleted()
    assert event.prompt_tokens is None
    assert event.completion_tokens is None


def test_generation_completed_carries_token_counts() -> None:
    event = GenerationCompleted(prompt_tokens=120, completion_tokens=17)
    assert event.prompt_tokens == 120
    assert event.completion_tokens == 17


def test_error_occurred() -> None:
    event = ErrorOccurred(message="boom")
    assert event.message == "boom"


def test_run_cancelled() -> None:
    assert RunCancelled() == RunCancelled()


def test_bus_event_exhaustive_match() -> None:
    def describe(event: BusEvent) -> str:
        match event:
            case RunStarted():
                return "run_started"
            case RunFinished():
                return "run_finished"
            case AssistantTextStarted():
                return "assistant_text_started"
            case AssistantTextDelta():
                return "assistant_text_delta"
            case AssistantTextFinished():
                return "assistant_text_finished"
            case AssistantThinkingStarted():
                return "assistant_thinking_started"
            case AssistantThinkingDelta():
                return "assistant_thinking_delta"
            case AssistantThinkingFinished():
                return "assistant_thinking_finished"
            case ToolCallStarted():
                return "tool_call_started"
            case ToolCallFinished():
                return "tool_call_finished"
            case GenerationCompleted():
                return "generation_completed"
            case ErrorOccurred():
                return "error_occurred"
            case RunCancelled():
                return "run_cancelled"

    assert describe(RunStarted()) == "run_started"
    assert describe(RunFinished()) == "run_finished"
    assert describe(AssistantTextStarted(id="1")) == "assistant_text_started"
    assert describe(AssistantTextDelta(id="1", text="hi")) == "assistant_text_delta"
    assert describe(AssistantTextFinished(id="1")) == "assistant_text_finished"
    assert describe(AssistantThinkingStarted(id="1")) == "assistant_thinking_started"
    assert describe(AssistantThinkingDelta(id="1", text="hmm")) == "assistant_thinking_delta"
    assert describe(AssistantThinkingFinished(id="1")) == "assistant_thinking_finished"
    assert describe(ToolCallStarted(id="1", name="echo", arguments={})) == "tool_call_started"
    call = ToolCall(id="1", name="echo", arguments={})
    assert describe(ToolCallFinished(id="1", tool_call=call, result="ok")) == "tool_call_finished"
    assert describe(GenerationCompleted()) == "generation_completed"
    assert describe(ErrorOccurred(message="boom")) == "error_occurred"
    assert describe(RunCancelled()) == "run_cancelled"
