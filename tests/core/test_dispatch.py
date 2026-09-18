import threading
from collections.abc import Mapping

from pico.core.actions import register_actions
from pico.core.bus import Bus
from pico.core.events import (
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.ledger import facts
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.dispatch import MAX_INVALID_ACTION_ATTEMPTS
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import Failed
from pico.core.stuckness import STUCK_THRESHOLD
from pico.core.tools import Tool, ToolRegistry
from pico.llm.types import (
    GenerationComplete,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)
from pico.session import (
    AssistantMessageRecorded,
    ToolCallRecorded,
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    answer_turn,
    echo_registry,
    failing_registry,
    make_session,
)


def test_unknown_tool_call_surfaced_as_tool_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="missing", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
            answer_turn(),
        ]
    )

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        GenerationCompleted(iteration=1),
        ToolCallStarted(id="0", name="missing", arguments={}),
        ToolCallFinished(id="0", tool_call=call, result="missing", is_error=True),
        GenerationCompleted(iteration=2),
    ]
    assert ToolCallRecorded(name="missing", arguments={}, result="missing", is_error=True) in list(
        session.events()
    )


def test_cancel_set_before_second_tool_call_leaves_it_unexecuted() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()

    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="first", description="first", parameters={"type": "object"}),
            execute=lambda args: (cancel.set(), "first done")[1],
        )
    )
    registry.register(
        Tool(
            spec=ToolSpec(name="second", description="second", parameters={"type": "object"}),
            execute=lambda args: "second done",
        )
    )
    first_call = ToolCall(id="1", name="first", arguments={})
    second_call = ToolCall(id="2", name="second", arguments={})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=first_call),
                ToolCallReady(tool_call=second_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events[:3] == [
        RunStarted(),
        GenerationCompleted(iteration=1),
        ToolCallStarted(id="0", name="first", arguments={}),
    ]
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.result == "first done"
    assert finished.is_error is False
    assert next(subscriber) == RunCancelled()
    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert [event.name for event in tool_events] == ["first"]


def test_tool_call_finished_fact_id_matches_facts_after_interleaved_events() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="earlier", arguments={}, result="r", is_error=False))
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.fact_id == facts(session)[-1].id


def test_failed_tool_call_has_no_fact_id() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="missing", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.fact_id is None


def test_bookkeeping_tool_call_finishes_without_a_fact_id() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="note", arguments={}, result="finding", is_error=False))
    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="read_fact", description="recall", parameters={"type": "object"}),
            execute=lambda args: "finding",
        )
    )
    call = ToolCall(id="1", name="read_fact", arguments={"id": 2})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.is_error is False
    assert finished.fact_id is None


def test_repeated_invalid_actions_stop_run_at_max_attempts() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="missing", arguments={"n": i})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(MAX_INVALID_ACTION_ATTEMPTS + 5)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    error_events = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.is_error
    ]
    assert len(error_events) == MAX_INVALID_ACTION_ATTEMPTS


def test_fewer_than_cap_invalid_actions_do_not_end_run_early() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="missing", arguments={})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert list(session.events())[-1] == AssistantMessageRecorded(content="done", thinking="")


def test_repeated_identical_action_hits_hard_stuckness_before_max_steps() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(STUCK_THRESHOLD + 5)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    repeats = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and not event.is_error
    ]
    assert len(repeats) == STUCK_THRESHOLD


def test_tool_error_is_recorded_as_error_and_excluded_from_facts() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="boom", arguments={})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, failing_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert recorded[0].is_error is True
    assert "could not read /nope" in recorded[0].result
    assert facts(session) == []


def test_repeated_tool_errors_do_not_end_run_via_invalid_action_cap() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="boom", arguments={"n": i})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(MAX_INVALID_ACTION_ATTEMPTS)
    ]
    turns.append([TextDelta(text="done"), GenerationComplete(finish_reason="stop")])
    client = ScriptedClient(turns)

    runner = LoopRunner(client, failing_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.dispatch.invalid_action_attempts == 0
    errors = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.is_error
    ]
    assert len(errors) == MAX_INVALID_ACTION_ATTEMPTS
    assert list(session.events())[-1] == AssistantMessageRecorded(content="done", thinking="")


def test_invalid_registry_tool_arguments_are_recorded_as_errors_not_facts() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="set_plan", arguments={})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    tools = ToolRegistry()
    register_actions(tools, session)

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert "missing required field" in recorded.result
    assert runner.dispatch.invalid_action_attempts == 1
    assert facts(session) == []


def test_unexpected_tool_exception_finishes_run_with_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))

    def blow_up(arguments: Mapping[str, object]) -> str:
        raise RuntimeError("wired wrong")

    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="echo", description="echo", parameters={"type": "object"}),
            execute=blow_up,
        )
    )
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="echo", arguments={})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert runner.state == Failed("wired wrong")
    assert events[-2] == ErrorOccurred(message="wired wrong")
    assert events[-1] == RunFinished(error="wired wrong")


def test_invalid_action_streak_resets_on_a_valid_call() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    invalid: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="missing", arguments={"n": i})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(2 * (MAX_INVALID_ACTION_ATTEMPTS - 1))
    ]
    valid: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="v", name="echo", arguments={"text": "hi"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    turns: list[list[StreamEvent]] = [
        *invalid[: MAX_INVALID_ACTION_ATTEMPTS - 1],
        valid,
        *invalid[MAX_INVALID_ACTION_ATTEMPTS - 1 :],
        [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(recorded) == 2 * (MAX_INVALID_ACTION_ATTEMPTS - 1) + 1
    assert runner.dispatch.invalid_action_attempts == MAX_INVALID_ACTION_ATTEMPTS - 1
