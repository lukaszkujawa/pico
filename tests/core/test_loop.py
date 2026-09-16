import threading
from collections.abc import Iterator

from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.loop import (
    DEFAULT_LOOP_CONFIG,
    MAX_INVALID_ACTION_ATTEMPTS,
    LoopConfig,
    LoopRunner,
    StepOutcome,
    stream_step,
    tool_call_step,
)
from pico.core.tools import Tool, ToolRegistry
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
    ToolSpec,
)
from pico.session import (
    AssistantMessageRecorded,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
    connect,
)


class ScriptedClient:
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield from self._turns.pop(0)


class FailingClient:
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        raise LLMError("connection lost")
        yield


class CancellingClient:
    def __init__(
        self, events: list[StreamEvent], cancel: threading.Event, cancel_after: int
    ) -> None:
        self._events = events
        self._cancel = cancel
        self._cancel_after = cancel_after

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        for index, event in enumerate(self._events):
            if index == self._cancel_after:
                self._cancel.set()
            yield event


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="echo", description="echo", parameters={"type": "object"}),
            execute=lambda args: str(args.get("text", "")),
        )
    )
    return registry


def _session(session_id: str = "s1") -> Session:
    conn = connect(":memory:")
    return Session(conn, session_id)


def test_step_outcome_accepts_each_literal_value() -> None:
    def make(value: StepOutcome) -> StepOutcome:
        return value

    assert make("continue") == "continue"
    assert make("done") == "done"
    assert make("cancelled") == "cancelled"


def test_loop_config_holds_ordered_steps_and_max_steps() -> None:
    def a(runner: LoopRunner) -> StepOutcome:
        return "done"

    def b(runner: LoopRunner) -> StepOutcome:
        return "done"

    config = LoopConfig(steps=(a, b), max_steps=5)

    assert config.steps == (a, b)
    assert config.max_steps == 5


def test_single_always_done_step_publishes_started_and_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def always_done(runner: LoopRunner) -> StepOutcome:
        return "done"

    runner = LoopRunner(
        FailingClient(), _echo_registry(), bus, _session(), LoopConfig(steps=(always_done,))
    )
    runner.execute()

    assert next(subscriber) == RunStarted()
    assert next(subscriber) == RunFinished()


def test_continue_n_times_then_done_runs_n_plus_one_iterations() -> None:
    bus = Bus()
    calls: list[int] = []

    def counting(runner: LoopRunner) -> StepOutcome:
        calls.append(1)
        return "continue" if len(calls) <= 2 else "done"

    runner = LoopRunner(
        FailingClient(), _echo_registry(), bus, _session(), LoopConfig(steps=(counting,))
    )
    runner.execute()

    assert len(calls) == 3


def test_cancelled_outcome_publishes_cancelled_not_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def always_cancelled(runner: LoopRunner) -> StepOutcome:
        return "cancelled"

    runner = LoopRunner(
        FailingClient(), _echo_registry(), bus, _session(), LoopConfig(steps=(always_cancelled,))
    )
    runner.execute()

    assert next(subscriber) == RunStarted()
    assert next(subscriber) == RunCancelled()


def test_step_raising_llm_error_surfaces_as_error_occurred() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def raising(runner: LoopRunner) -> StepOutcome:
        raise LLMError("connection lost")

    runner = LoopRunner(
        FailingClient(), _echo_registry(), bus, _session(), LoopConfig(steps=(raising,))
    )
    runner.execute()

    assert next(subscriber) == RunStarted()
    assert next(subscriber) == ErrorOccurred(message="connection lost")
    assert next(subscriber) == RunFinished(error="connection lost")


def test_max_steps_reached_without_terminal_outcome_publishes_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    calls: list[int] = []

    def always_continue(runner: LoopRunner) -> StepOutcome:
        calls.append(1)
        return "continue"

    runner = LoopRunner(
        FailingClient(),
        _echo_registry(),
        bus,
        _session(),
        LoopConfig(steps=(always_continue,), max_steps=3),
    )
    runner.execute()

    assert len(calls) == 3
    assert next(subscriber) == RunStarted()
    assert next(subscriber) == RunFinished()


def test_steps_after_non_continue_step_are_not_called() -> None:
    bus = Bus()
    calls: list[str] = []

    def first(runner: LoopRunner) -> StepOutcome:
        calls.append("first")
        return "done"

    def second(runner: LoopRunner) -> StepOutcome:
        calls.append("second")
        return "done"

    runner = LoopRunner(
        FailingClient(), _echo_registry(), bus, _session(), LoopConfig(steps=(first, second))
    )
    runner.execute()

    assert calls == ["first"]


def test_plain_text_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                TextDelta(text="hello "),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="hello "),
        AssistantTextDelta(id="0", text="world"),
        AssistantTextFinished(id="0"),
        RunFinished(),
    ]
    assert list(session.events()) == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="hello world", thinking=""),
    ]


def test_thinking_then_text_published_in_order_with_shared_ids_across_two_turns() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ThinkingDelta(text="pondering "),
                ThinkingDelta(text="more"),
                TextDelta(text="hello "),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ],
            [
                ThinkingDelta(text="second thought"),
                TextDelta(text="second answer"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    first_events = [next(subscriber) for _ in range(10)]
    assert first_events == [
        RunStarted(),
        AssistantThinkingStarted(id="0"),
        AssistantThinkingDelta(id="0", text="pondering "),
        AssistantThinkingDelta(id="0", text="more"),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="hello "),
        AssistantTextDelta(id="1", text="world"),
        AssistantThinkingFinished(id="0"),
        AssistantTextFinished(id="1"),
        RunFinished(),
    ]

    runner.execute()

    second_events = [next(subscriber) for _ in range(7)]
    started_ids_second = [
        event.id
        for event in second_events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted)
    ]
    assert started_ids_second == ["2", "3"]

    assert list(session.events()) == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="hello world", thinking="pondering more"),
        AssistantMessageRecorded(content="second answer", thinking="second thought"),
    ]


def test_single_tool_call_round_trip() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(7)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="echo"),
        ToolCallFinished(id="1", tool_call=call, result="hi", is_error=False),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="done"),
        AssistantTextFinished(id="0"),
        RunFinished(),
    ]
    assert list(session.events()) == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="", thinking=""),
        ToolCallRecorded(name="echo", arguments={"text": "hi"}, result="hi", is_error=False),
        AssistantMessageRecorded(content="done", thinking=""),
    ]


def test_tool_call_arguments_delta_forwarded() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [
                ToolCallDelta(id="1", name="echo", arguments_delta='{"text":'),
                ToolCallReady(tool_call=call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        ToolCallArgumentsDelta(id="1", arguments_delta='{"text":'),
        ToolCallStarted(id="1", name="echo"),
        ToolCallFinished(id="1", tool_call=call, result="hi", is_error=False),
        RunFinished(),
    ]


def test_unknown_tool_call_surfaced_as_tool_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="missing", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, ToolRegistry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="missing"),
        ToolCallFinished(id="1", tool_call=call, result="missing", is_error=True),
        RunFinished(),
    ]
    assert ToolCallRecorded(name="missing", arguments={}, result="missing", is_error=True) in list(
        session.events()
    )


def test_llm_error_surfaced_as_error_occurred() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = FailingClient()

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(3)]
    assert events == [
        RunStarted(),
        ErrorOccurred(message="connection lost"),
        RunFinished(error="connection lost"),
    ]


def test_cancel_set_before_streaming_stops_immediately() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    cancel.set()
    client = CancellingClient(
        events=[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")],
        cancel=cancel,
        cancel_after=-1,
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG, cancel)
    runner.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]
    assert list(session.events()) == [UserMessageRecorded(content="hi")]


def test_cancel_mid_stream_stops_consuming_and_closes_open_panes() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    client = CancellingClient(
        events=[
            ThinkingDelta(text="pondering"),
            TextDelta(text="hel"),
            TextDelta(text="lo"),
            GenerationComplete(finish_reason="stop"),
        ],
        cancel=cancel,
        cancel_after=2,
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG, cancel)
    runner.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        AssistantThinkingStarted(id="0"),
        AssistantThinkingDelta(id="0", text="pondering"),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="hel"),
        AssistantThinkingFinished(id="0"),
    ]
    assert next(subscriber) == AssistantTextFinished(id="1")
    assert next(subscriber) == RunCancelled()


def test_cancelled_turn_does_not_publish_run_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    client = CancellingClient(
        events=[TextDelta(text="hi"), GenerationComplete(finish_reason="stop")],
        cancel=cancel,
        cancel_after=0,
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG, cancel)
    runner.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]
    assert not any(isinstance(event, RunFinished) for event in events)


def test_default_loop_config_is_stream_then_tool_call() -> None:
    assert DEFAULT_LOOP_CONFIG.steps == (stream_step, tool_call_step)
    assert DEFAULT_LOOP_CONFIG.max_steps is None


def test_valid_answer_call_ends_run_and_records_result() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": []})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="answer"),
        ToolCallFinished(id="1", tool_call=call, result="the answer", is_error=False),
        RunFinished(),
    ]
    assert runner.final_answer == "the answer"
    assert list(session.events())[-1] == ToolCallRecorded(
        name="answer",
        arguments={"content": "the answer", "citations": []},
        result="the answer",
        is_error=False,
    )


def test_invalid_answer_call_continues_run_instead_of_ending() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    bad_call = ToolCall(id="1", name="answer", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=bad_call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(3)]
    assert events[:2] == [RunStarted(), ToolCallStarted(id="1", name="answer")]
    finished = events[2]
    assert isinstance(finished, ToolCallFinished)
    assert finished.is_error is True
    assert runner.final_answer is None


def test_answer_citing_known_fact_ends_run() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="content", is_error=False)
    )
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [0]})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "the answer"


def test_answer_citing_unknown_fact_continues_run() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [0]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result


def test_repeated_invalid_actions_stop_run_at_max_attempts() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="missing", arguments={})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(MAX_INVALID_ACTION_ATTEMPTS + 5)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, ToolRegistry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    error_events = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.is_error
    ]
    assert len(error_events) == MAX_INVALID_ACTION_ATTEMPTS


def test_fewer_than_cap_invalid_actions_do_not_end_run_early() -> None:
    bus = Bus()
    session = _session()
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

    runner = LoopRunner(client, ToolRegistry(), bus, session, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert list(session.events())[-1] == AssistantMessageRecorded(content="done", thinking="")
