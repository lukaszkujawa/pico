import itertools
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
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.ledger import facts
from pico.core.loop import (
    DEFAULT_LOOP_CONFIG,
    MAX_DELEGATE_STEPS,
    MAX_INVALID_ACTION_ATTEMPTS,
    LoopConfig,
    LoopRunner,
    StepOutcome,
    stream_step,
    stuckness_step,
    tool_call_step,
)
from pico.core.stuckness import NUDGE_THRESHOLD, STUCK_THRESHOLD
from pico.core.tools import Tool, ToolRegistry
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
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
        self.seen_messages: list[list[Message]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(messages)
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
        FailingClient(),
        _echo_registry(),
        bus,
        _session(),
        128_000,
        LoopConfig(steps=(always_done,)),
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
        FailingClient(), _echo_registry(), bus, _session(), 128_000, LoopConfig(steps=(counting,))
    )
    runner.execute()

    assert len(calls) == 3


def test_cancelled_outcome_publishes_cancelled_not_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def always_cancelled(runner: LoopRunner) -> StepOutcome:
        return "cancelled"

    runner = LoopRunner(
        FailingClient(),
        _echo_registry(),
        bus,
        _session(),
        128_000,
        LoopConfig(steps=(always_cancelled,)),
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
        FailingClient(), _echo_registry(), bus, _session(), 128_000, LoopConfig(steps=(raising,))
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
        128_000,
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
        FailingClient(),
        _echo_registry(),
        bus,
        _session(),
        128_000,
        LoopConfig(steps=(first, second)),
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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


def test_shared_id_source_keeps_ids_unique_across_separate_runners() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    id_source = itertools.count()

    first_client = ScriptedClient(
        [
            [
                ThinkingDelta(text="pondering"),
                TextDelta(text="hello"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )
    first_runner = LoopRunner(
        first_client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, None, id_source
    )
    first_runner.execute()

    second_client = ScriptedClient(
        [
            [
                ThinkingDelta(text="more thoughts"),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )
    second_runner = LoopRunner(
        second_client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, None, id_source
    )
    second_runner.execute()

    all_events = [next(subscriber) for _ in range(16)]
    started_ids = [
        event.id
        for event in all_events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted)
    ]
    assert started_ids == ["0", "1", "2", "3"]
    assert len(set(started_ids)) == len(started_ids)


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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(7)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="echo", arguments={"text": "hi"}),
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


def test_tool_call_delta_from_llm_is_ignored() -> None:
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="echo", arguments={"text": "hi"}),
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

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="missing", arguments={}),
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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

    runner = LoopRunner(
        client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
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

    runner = LoopRunner(
        client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
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

    runner = LoopRunner(
        client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
    runner.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]
    assert not any(isinstance(event, RunFinished) for event in events)


def test_default_loop_config_is_stuckness_then_stream_then_tool_call() -> None:
    assert DEFAULT_LOOP_CONFIG.steps == (stuckness_step, stream_step, tool_call_step)
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events == [
        RunStarted(),
        ToolCallStarted(
            id="1", name="answer", arguments={"content": "the answer", "citations": []}
        ),
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(3)]
    assert events[:2] == [
        RunStarted(),
        ToolCallStarted(id="1", name="answer", arguments={}),
    ]
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert list(session.events())[-1] == AssistantMessageRecorded(content="done", thinking="")


def test_stream_step_sends_budget_rendered_messages_to_llm() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="x" * 10_000, is_error=False)
    )
    client = ScriptedClient([[TextDelta(text="ok"), GenerationComplete(finish_reason="stop")]])

    runner = LoopRunner(client, ToolRegistry(), bus, session, 100, DEFAULT_LOOP_CONFIG)
    runner.execute()

    sent_tool_messages = [m for m in client.seen_messages[0] if m.role.value == "tool"]
    assert sent_tool_messages[0].tool_result is not None
    assert sent_tool_messages[0].tool_result.content != "x" * 10_000


def test_delegate_call_that_answers_records_fact_on_parent() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert tool_events[-1] == ToolCallRecorded(
        name="delegate",
        arguments={"question": "what is x?"},
        result="x is 1",
        is_error=False,
    )
    parent_facts = facts(session)
    assert parent_facts[-1].content == "x is 1"
    assert parent_facts[-1].source == "delegate"


def test_delegate_call_exhausting_budget_without_answer_is_error() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    turns: list[list[StreamEvent]] = [
        [ToolCallReady(tool_call=delegate_call), GenerationComplete(finish_reason="tool_calls")],
    ]
    turns.extend(
        [TextDelta(text="thinking"), GenerationComplete(finish_reason="stop")]
        for _ in range(MAX_DELEGATE_STEPS)
    )
    client = ScriptedClient(turns)

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert tool_events[-1].name == "delegate"
    assert tool_events[-1].is_error is True
    assert facts(session) == []


def test_delegate_sub_agent_cannot_use_mutating_tools() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "write a file"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": "a.txt", "content": "x"},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2",
                        name="answer",
                        arguments={"content": "cannot write", "citations": []},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_session = session.child("delegate/1")
    child_tool_events = [
        event for event in child_session.events() if isinstance(event, ToolCallRecorded)
    ]
    assert child_tool_events[0].name == "write_file"
    assert child_tool_events[0].is_error is True


def test_delegate_child_session_is_distinct_from_parent() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_session = session.child("delegate/1")
    child_user_messages = [
        event for event in child_session.events() if isinstance(event, UserMessageRecorded)
    ]
    assert child_user_messages == [UserMessageRecorded(content="what is x?")]
    parent_user_messages = [
        event for event in session.events() if isinstance(event, UserMessageRecorded)
    ]
    assert parent_user_messages == [UserMessageRecorded(content="hi")]


def test_delegate_child_stream_events_do_not_appear_on_parent_bus() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                TextDelta(text="pondering"),
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="delegate", arguments={"question": "what is x?"}),
        ToolCallFinished(id="1", tool_call=delegate_call, result="x is 1", is_error=False),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="done"),
        AssistantTextFinished(id="0"),
    ]


def test_repeated_identical_action_hits_hard_stuckness_before_max_steps() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(STUCK_THRESHOLD + 5)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(tool_events) == STUCK_THRESHOLD


def test_nudge_below_stuck_threshold_is_appended_as_user_message_not_persisted() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(NUDGE_THRESHOLD)
    ]
    turns.append([TextDelta(text="done"), GenerationComplete(finish_reason="stop")])
    client = ScriptedClient(turns)

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    last_call_messages = client.seen_messages[-1]
    assert last_call_messages[-1].role is Role.USER
    assert "repeated" in last_call_messages[-1].content

    assert not any(
        isinstance(event, UserMessageRecorded) and "repeated" in event.content
        for event in session.events()
    )


def test_no_nudge_below_threshold_behaves_as_before() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")]])

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    sent_messages = client.seen_messages[0]
    assert all(
        message.role is not Role.USER or message.content == "hi" for message in sent_messages
    )
