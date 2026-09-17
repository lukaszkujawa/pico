from collections.abc import Iterator

from pico.core.stuckness import STUCK_THRESHOLD
from pico.headless import TurnResult, run_turn
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)
from pico.session import Session, ToolCallRecorded, connect


class ScriptedClient:
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield from self._turns.pop(0)


class FailingClient:
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        raise LLMError("connection lost")
        yield


class LoopingClient:
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield ToolCallReady(
            tool_call=ToolCall(id="c", name="read_file", arguments={"path": "/nope"})
        )
        yield GenerationComplete(finish_reason="tool_calls")


def _session() -> Session:
    return Session(connect(":memory:"), "s1")


def _answer(content: str, usage: GenerationComplete | None = None) -> list[StreamEvent]:
    return [
        ToolCallReady(
            tool_call=ToolCall(
                id="a", name="answer", arguments={"content": content, "citations": []}
            )
        ),
        usage or GenerationComplete(finish_reason="tool_calls"),
    ]


def test_turn_ending_in_answer_returns_it_with_counts() -> None:
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="shell", arguments={"command": "echo hi"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            _answer("done"),
        ]
    )

    result = run_turn(client, _session(), 128_000, "do it")

    assert result.answer == "done"
    assert result.iterations == 2
    assert result.tool_calls == 2
    assert result.error is None
    assert result.duration_seconds >= 0


def test_plain_text_turn_without_answer_yields_no_answer() -> None:
    client = ScriptedClient([[TextDelta(text="thinking out loud")]])

    result = run_turn(client, _session(), 128_000, "do it")

    assert result == TurnResult(
        answer=None,
        iterations=1,
        tool_calls=0,
        prompt_tokens=0,
        completion_tokens=0,
        duration_seconds=result.duration_seconds,
        error=None,
    )


def test_turn_ending_by_stuckness_yields_no_answer() -> None:
    session = _session()

    result = run_turn(LoopingClient(), session, 128_000, "do it")

    assert result.answer is None
    assert result.error is not None
    assert "stuck" in result.error
    repeats = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_file"
    ]
    assert len(repeats) == STUCK_THRESHOLD


def test_token_counts_sum_across_a_multi_call_turn() -> None:
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="shell", arguments={"command": "true"})
                ),
                GenerationComplete(
                    finish_reason="tool_calls", prompt_tokens=10, completion_tokens=3
                ),
            ],
            _answer(
                "done",
                GenerationComplete(
                    finish_reason="tool_calls", prompt_tokens=20, completion_tokens=7
                ),
            ),
        ]
    )

    result = run_turn(client, _session(), 128_000, "do it")

    assert result.prompt_tokens == 30
    assert result.completion_tokens == 10


def test_missing_token_counts_sum_as_zero() -> None:
    client = ScriptedClient([_answer("done")])

    result = run_turn(client, _session(), 128_000, "do it")

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


def test_llm_error_surfaces_in_error_field() -> None:
    result = run_turn(FailingClient(), _session(), 128_000, "do it")

    assert result.error == "connection lost"
    assert result.answer is None
