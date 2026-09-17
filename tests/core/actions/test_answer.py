import pytest

from pico.core.actions import (
    RUNNER_ACTIONS,
    Answer,
    InvalidActionError,
)
from pico.core.bus import Bus
from pico.core.events import (
    AnswerSettled,
    GenerationCompleted,
    RunFinished,
    RunStarted,
    ToolCallStarted,
)
from pico.core.ledger import facts
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.runner import LoopRunner
from pico.llm.types import (
    GenerationComplete,
    TextDelta,
    ToolCall,
    ToolCallReady,
)
from pico.session import (
    ToolCallRecorded,
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    echo_registry,
    make_session,
)


def test_answer_from_arguments() -> None:
    action = Answer.from_arguments({"content": "the answer", "citations": [0, 1]})
    assert action == Answer(content="the answer", citations=(0, 1))


def test_answer_from_arguments_missing_content() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"citations": []})


def test_answer_from_arguments_missing_citations() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer"})


def test_answer_from_arguments_citations_not_a_list() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer", "citations": "0"})


def test_answer_from_arguments_citations_with_non_int_element() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer", "citations": [0, "1"]})


def test_answer_from_arguments_without_verify_is_none() -> None:
    action = Answer.from_arguments({"content": "the answer", "citations": []})
    assert action.verify is None


def test_answer_from_arguments_with_verify_round_trips() -> None:
    action = Answer.from_arguments(
        {"content": "the answer", "citations": [], "verify": "test -f out.txt"}
    )
    assert action == Answer(content="the answer", citations=(), verify="test -f out.txt")


def test_answer_from_arguments_verify_not_a_string() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer", "citations": [], "verify": 1})


def test_answer_from_arguments_verify_empty_string() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        Answer.from_arguments({"content": "the answer", "citations": [], "verify": "   "})


def test_valid_answer_call_ends_run_and_records_result() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": []})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(
            id="0", name="answer", arguments={"content": "the answer", "citations": []}
        ),
        AnswerSettled(id="0", content="the answer", accepted=True, reason=None, verify=None),
        RunFinished(),
    ]
    assert runner.final_answer == "the answer"
    assert list(session.events())[-1] == ToolCallRecorded(
        name="answer",
        arguments={"content": "the answer", "citations": []},
        result="the answer",
        is_error=False,
    )


def test_answer_citing_a_bookkeeping_call_seq_is_rejected() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="note", arguments={}, result="finding", is_error=False))
    session.append(
        ToolCallRecorded(name="read_fact", arguments={"id": 2}, result="finding", is_error=False)
    )
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [3]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result


def test_invalid_answer_call_continues_run_instead_of_ending() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    bad_call = ToolCall(id="1", name="answer", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=bad_call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events[:3] == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="answer", arguments={}),
    ]
    settled = events[3]
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.reason == "missing required field 'content'"
    assert runner.final_answer is None


def test_answer_citing_known_fact_ends_run() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="content", is_error=False)
    )
    fact_id = facts(session)[0].id
    call = ToolCall(
        id="1", name="answer", arguments={"content": "the answer", "citations": [fact_id]}
    )
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "the answer"


def test_answer_citing_unknown_fact_continues_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [0]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    settled = next(event for event in events if isinstance(event, AnswerSettled))
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.reason is not None
    assert "unknown fact citation" in settled.reason


def test_answer_citing_seq_of_error_call_continues_run() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [2]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result


def test_answer_spec_documents_verify() -> None:
    spec = RUNNER_ACTIONS["answer"].spec
    properties = spec.parameters["properties"]
    required = spec.parameters["required"]
    assert isinstance(properties, dict)
    assert isinstance(required, list)
    assert "verify" in properties
    assert "verify" not in required
    assert "exits 0" in spec.description
