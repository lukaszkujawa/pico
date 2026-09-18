from collections.abc import Mapping

import pytest

from pico.core.actions import (
    RUNNER_ACTIONS,
    Answer,
    InvalidActionError,
)
from pico.core.actions.answer import INLINE_CITATION_INDEX_MAX
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
from pico.core.loop.dispatch import MAX_INVALID_ACTION_ATTEMPTS
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import Answered
from pico.llm.types import (
    GenerationComplete,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
)
from pico.session import (
    Session,
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
        GenerationCompleted(iteration=1),
        ToolCallStarted(
            id="0", name="answer", arguments={"content": "the answer", "citations": []}
        ),
        AnswerSettled(id="0", content="the answer", accepted=True, reason=None, verify=None),
        RunFinished(),
    ]
    assert runner.state == Answered("the answer")
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

    assert not isinstance(runner.state, Answered)
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
        GenerationCompleted(iteration=1),
        ToolCallStarted(id="0", name="answer", arguments={}),
    ]
    settled = events[3]
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.reason == "missing required field 'content'"
    assert not isinstance(runner.state, Answered)


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

    assert runner.state == Answered("the answer")


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

    assert not isinstance(runner.state, Answered)
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

    assert not isinstance(runner.state, Answered)
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


def _answer_call(call_id: str, arguments: Mapping[str, object]) -> list[StreamEvent]:
    return [
        ToolCallReady(tool_call=ToolCall(id=call_id, name="answer", arguments=arguments)),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def _last_result(session: Session) -> str:
    return [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1].result


def _session_with_facts(count: int) -> Session:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    for index in range(count):
        session.append(
            ToolCallRecorded(
                name="read_file",
                arguments={"path": f"f{index}.txt"},
                result=f"content {index}",
                is_error=False,
            )
        )
    return session


def test_unknown_citation_rejection_inlines_the_fact_index_for_a_small_ledger() -> None:
    session = _session_with_facts(2)
    client = ScriptedClient(
        [
            _answer_call("1", {"content": "the answer", "citations": [39]}),
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    result = _last_result(session)
    assert "[39] does not exist" in result
    for fact in facts(session):
        assert f"[{fact.id}] read_file" in result
    assert result.endswith(
        "Keep your answer's content, fix only the citations, and drop any claim no fact supports."
    )


def test_repaired_answer_citing_a_listed_id_is_accepted() -> None:
    session = _session_with_facts(2)
    listed = facts(session)[0].id
    client = ScriptedClient(
        [
            _answer_call("1", {"content": "the answer", "citations": [39]}),
            _answer_call("2", {"content": "the answer", "citations": [listed]}),
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("the answer")


def test_unknown_citation_rejection_names_search_facts_for_a_large_ledger() -> None:
    session = _session_with_facts(INLINE_CITATION_INDEX_MAX + 1)
    client = ScriptedClient(
        [
            _answer_call("1", {"content": "the answer", "citations": [999]}),
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    result = _last_result(session)
    assert "search_facts(" in result
    assert "read_fact(id) to recover" not in result
    assert "fix only the citations" in result


def test_missing_citations_field_is_rejected_with_the_teaching_message() -> None:
    session = _session_with_facts(2)
    client = ScriptedClient(
        [
            _answer_call("1", {"content": "the answer"}),
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    result = _last_result(session)
    assert "citations" in result
    assert "fix only the citations" in result
    assert not isinstance(runner.state, Answered)


def test_malformed_citations_field_is_rejected_with_the_teaching_message() -> None:
    session = _session_with_facts(2)
    client = ScriptedClient(
        [
            _answer_call("1", {"content": "the answer", "citations": "39"}),
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert "fix only the citations" in _last_result(session)


def test_citation_rejection_with_an_empty_ledger_says_no_facts_exist() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            _answer_call("1", {"content": "the answer", "citations": [0]}),
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    result = _last_result(session)
    assert "No facts exist yet to cite" in result
    assert "fix only the citations" in result


def test_citation_rejections_do_not_count_as_invalid_actions() -> None:
    session = _session_with_facts(1)
    turns = [
        _answer_call(str(index), {"content": "the answer", "citations": [999]})
        for index in range(MAX_INVALID_ACTION_ATTEMPTS)
    ]
    turns.append([TextDelta(text="done"), GenerationComplete(finish_reason="stop")])
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.dispatch.invalid_action_attempts == 0
    errors = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.is_error
    ]
    assert len(errors) == MAX_INVALID_ACTION_ATTEMPTS


def test_missing_content_field_is_still_an_invalid_action() -> None:
    session = _session_with_facts(1)
    client = ScriptedClient(
        [
            _answer_call("1", {"citations": []}),
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.dispatch.invalid_action_attempts == 1
    assert _last_result(session) == "missing required field 'content'"
