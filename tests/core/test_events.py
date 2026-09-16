from pico.core.events import (
    AnswerSettled,
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BudgetExceeded,
    BusEvent,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallResultDelta,
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


def test_tool_call_arguments_delta() -> None:
    event = ToolCallArgumentsDelta(id="1", name="read_file", text='{"path":')
    assert event.id == "1"
    assert event.name == "read_file"
    assert event.text == '{"path":'


def test_tool_call_result_delta() -> None:
    event = ToolCallResultDelta(id="1", text="line 1\n")
    assert event.id == "1"
    assert event.text == "line 1\n"


def test_tool_call_finished() -> None:
    call = ToolCall(id="1", name="echo", arguments={})
    event = ToolCallFinished(id="1", tool_call=call, result="ok")
    assert event.tool_call == call
    assert event.result == "ok"
    assert event.is_error is False


def test_answer_settled_accepted_defaults() -> None:
    event = AnswerSettled(id="1", content="the answer", accepted=True)
    assert event.reason is None
    assert event.verify is None


def test_answer_settled_rejected_carries_reason() -> None:
    event = AnswerSettled(
        id="1", content="the answer", accepted=False, reason="unknown fact citation(s): [3]"
    )
    assert event.accepted is False
    assert event.reason == "unknown fact citation(s): [3]"


def test_answer_settled_carries_verify_command() -> None:
    event = AnswerSettled(id="1", content="done", accepted=True, verify="pytest")
    assert event.verify == "pytest"


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


def test_budget_exceeded_carries_estimate_actual_and_budget() -> None:
    event = BudgetExceeded(estimated=900, actual=1500, budget=1200)

    assert (event.estimated, event.actual, event.budget) == (900, 1500, 1200)


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
            case ToolCallArgumentsDelta():
                return "tool_call_arguments_delta"
            case ToolCallResultDelta():
                return "tool_call_result_delta"
            case ToolCallFinished():
                return "tool_call_finished"
            case AnswerSettled():
                return "answer_settled"
            case GenerationCompleted():
                return "generation_completed"
            case ErrorOccurred():
                return "error_occurred"
            case RunCancelled():
                return "run_cancelled"
            case BudgetExceeded():
                return "budget_exceeded"

    assert describe(RunStarted()) == "run_started"
    assert describe(RunFinished()) == "run_finished"
    assert describe(AssistantTextStarted(id="1")) == "assistant_text_started"
    assert describe(AssistantTextDelta(id="1", text="hi")) == "assistant_text_delta"
    assert describe(AssistantTextFinished(id="1")) == "assistant_text_finished"
    assert describe(AssistantThinkingStarted(id="1")) == "assistant_thinking_started"
    assert describe(AssistantThinkingDelta(id="1", text="hmm")) == "assistant_thinking_delta"
    assert describe(AssistantThinkingFinished(id="1")) == "assistant_thinking_finished"
    assert describe(ToolCallStarted(id="1", name="echo", arguments={})) == "tool_call_started"
    assert (
        describe(ToolCallArgumentsDelta(id="1", name="echo", text="a"))
        == "tool_call_arguments_delta"
    )
    assert describe(ToolCallResultDelta(id="1", text="a")) == "tool_call_result_delta"
    call = ToolCall(id="1", name="echo", arguments={})
    assert describe(ToolCallFinished(id="1", tool_call=call, result="ok")) == "tool_call_finished"
    assert describe(AnswerSettled(id="1", content="ok", accepted=True)) == "answer_settled"
    assert describe(GenerationCompleted()) == "generation_completed"
    assert describe(ErrorOccurred(message="boom")) == "error_occurred"
    assert describe(RunCancelled()) == "run_cancelled"
    assert describe(BudgetExceeded(estimated=1, actual=2, budget=1)) == "budget_exceeded"
