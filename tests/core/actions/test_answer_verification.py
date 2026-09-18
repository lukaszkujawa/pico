from pathlib import Path
from unittest.mock import patch

from pico.core.bus import Bus
from pico.core.errors import ToolError
from pico.core.events import (
    AnswerSettled,
    RunFinished,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import Answered, Failed
from pico.core.stuckness import STUCK_THRESHOLD
from pico.llm.types import (
    GenerationComplete,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
)
from pico.session import (
    AssistantMessageRecorded,
    ToolCallRecorded,
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    echo_registry,
    make_session,
)


def test_answer_with_passing_verification_ends_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1", name="answer", arguments={"content": "done", "citations": [], "verify": "true"}
    )
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("done")
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is False
    assert recorded.result == "done\n\nverified: true"

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    settled = next(event for event in events if isinstance(event, AnswerSettled))
    assert isinstance(settled, AnswerSettled)
    assert (settled.content, settled.accepted, settled.reason, settled.verify) == (
        "done",
        True,
        None,
        "true",
    )
    assert "verified:" not in settled.content


def test_answer_with_failing_verification_is_rejected_and_run_continues() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1",
        name="answer",
        arguments={
            "content": "done",
            "citations": [],
            "verify": "echo missing output >&2; exit 3",
        },
    )
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="fixing"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert not isinstance(runner.state, Answered)
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is True
    assert "verification failed (exit 3)" in recorded.result
    assert "missing output" in recorded.result
    assert list(session.events())[-1] == AssistantMessageRecorded(content="fixing", thinking="")

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    settled = next(event for event in events if isinstance(event, AnswerSettled))
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.verify == "echo missing output >&2; exit 3"
    assert settled.reason is not None
    assert "verification failed (exit 3)" in settled.reason
    assert "missing output" in settled.reason


def test_answer_verification_sees_the_working_directory(tmp_path: Path) -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    target = tmp_path / "greeting.txt"
    call = ToolCall(
        id="1",
        name="answer",
        arguments={
            "content": "wrote it",
            "citations": [],
            "verify": f"test -f {target}",
        },
    )
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="fixing"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert not isinstance(runner.state, Answered)

    target.write_text("hello")
    session = make_session("s2")
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )
    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("wrote it")


def test_answer_without_verification_result_is_the_content_alone() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "done", "citations": []})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("done")
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.result == "done"
    assert recorded.is_error is False


def test_repeated_failing_verification_hits_stuckness_not_invalid_action_cap() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id=str(index),
                    name="answer",
                    arguments={"content": f"done {index}", "citations": [], "verify": "false"},
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for index in range(STUCK_THRESHOLD + 3)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(recorded) == STUCK_THRESHOLD + 1
    assert all(event.is_error for event in recorded)
    assert isinstance(runner.state, Failed)
    assert "stuck" in runner.state.reason


def test_answer_verification_timeout_is_a_tool_error_not_an_invalid_action() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1", name="answer", arguments={"content": "done", "citations": [], "verify": "sleep 5"}
    )
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="fixing"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    with patch(
        "pico.core.actions.shell.Shell.run", side_effect=ToolError("command timed out after 30s")
    ):
        runner.execute()

    assert not isinstance(runner.state, Answered)
    assert runner.dispatch.invalid_action_attempts == 0
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is True
    assert "timed out" in recorded.result
