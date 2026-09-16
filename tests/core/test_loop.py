import itertools
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest.mock import patch

from pico.core.actions import register_actions
from pico.core.bus import Bus
from pico.core.context import (
    SYSTEM_PROMPT,
    estimate_tokens,
    message_text,
    prompt_budget,
    render_messages,
)
from pico.core.errors import ToolError
from pico.core.events import (
    AnswerSettled,
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BudgetExceeded,
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
from pico.core.ledger import facts
from pico.core.loop import (
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_LOOP_CONFIG,
    MAX_CHARS_PER_TOKEN,
    MAX_DELEGATE_STEPS,
    MAX_INVALID_ACTION_ATTEMPTS,
    MIN_CHARS_PER_TOKEN,
    LoopConfig,
    LoopRunner,
    StepOutcome,
    specs_text,
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
    PlanSet,
    PlanStepCompleted,
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


def _failing_registry() -> ToolRegistry:
    def boom(arguments: Mapping[str, object]) -> str:
        raise ToolError("could not read /nope: no such file")

    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="boom", description="boom", parameters={"type": "object"}),
            execute=boom,
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

    events = [next(subscriber) for _ in range(7)]
    assert events == [
        RunStarted(),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="hello "),
        AssistantTextDelta(id="0", text="world"),
        GenerationCompleted(),
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

    first_events = [next(subscriber) for _ in range(11)]
    assert first_events == [
        RunStarted(),
        AssistantThinkingStarted(id="0"),
        AssistantThinkingDelta(id="0", text="pondering "),
        AssistantThinkingDelta(id="0", text="more"),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="hello "),
        AssistantTextDelta(id="1", text="world"),
        GenerationCompleted(),
        AssistantThinkingFinished(id="0"),
        AssistantTextFinished(id="1"),
        RunFinished(),
    ]

    runner.execute()

    second_events = [next(subscriber) for _ in range(8)]
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

    all_events = [next(subscriber) for _ in range(18)]
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

    events = [next(subscriber) for _ in range(9)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="echo", arguments={"text": "hi"}),
        ToolCallFinished(id="0", tool_call=call, result="hi", is_error=False, fact_id=3),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="done"),
        GenerationCompleted(),
        AssistantTextFinished(id="1"),
        RunFinished(),
    ]
    assert list(session.events()) == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="", thinking=""),
        ToolCallRecorded(name="echo", arguments={"text": "hi"}, result="hi", is_error=False),
        AssistantMessageRecorded(content="done", thinking=""),
    ]


def test_two_tool_calls_sharing_model_id_get_distinct_pane_ids() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    first_call = ToolCall(id="0", name="echo", arguments={"text": "first"})
    second_call = ToolCall(id="0", name="echo", arguments={"text": "second"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=first_call),
                ToolCallReady(tool_call=second_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(7)]
    started = [event for event in events if isinstance(event, ToolCallStarted)]
    finished = [event for event in events if isinstance(event, ToolCallFinished)]
    assert [event.id for event in started] == ["0", "1"]
    assert [event.id for event in finished] == ["0", "1"]
    assert started[0].id == finished[0].id
    assert started[1].id == finished[1].id


def test_concurrent_tool_call_deltas_land_on_separate_pane_ids() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    first_call = ToolCall(id="a", name="echo", arguments={"text": "first"})
    second_call = ToolCall(id="b", name="echo", arguments={"text": "second"})
    client = ScriptedClient(
        [
            [
                ToolCallDelta(id="a", name="echo", arguments_delta='{"text":'),
                ToolCallDelta(id="b", name="echo", arguments_delta='{"text":'),
                ToolCallDelta(id="a", name="echo", arguments_delta=' "first"}'),
                ToolCallDelta(id="b", name="echo", arguments_delta=' "second"}'),
                ToolCallReady(tool_call=first_call),
                ToolCallReady(tool_call=second_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(12)]
    deltas = [event for event in events if isinstance(event, ToolCallArgumentsDelta)]
    started = [event for event in events if isinstance(event, ToolCallStarted)]
    finished = [event for event in events if isinstance(event, ToolCallFinished)]

    first_pane_ids = {deltas[0].id, deltas[2].id}
    second_pane_ids = {deltas[1].id, deltas[3].id}
    assert len(first_pane_ids) == 1
    assert len(second_pane_ids) == 1
    assert first_pane_ids != second_pane_ids
    assert [event.id for event in started] == [deltas[0].id, deltas[1].id]
    assert [event.id for event in finished] == [deltas[0].id, deltas[1].id]


def test_tool_call_pane_ids_do_not_collide_with_text_or_thinking_pane_ids() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="7", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [
                ThinkingDelta(text="pondering"),
                TextDelta(text="calling"),
                ToolCallReady(tool_call=call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(9)]
    pane_ids = [
        event.id
        for event in events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted | ToolCallStarted)
    ]
    assert len(pane_ids) == len(set(pane_ids))


def test_tool_call_delta_from_llm_is_forwarded_with_the_calls_pane_id() -> None:
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

    events = [next(subscriber) for _ in range(7)]
    assert events == [
        RunStarted(),
        ToolCallArgumentsDelta(id="0", text='{"text":'),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="echo", arguments={"text": "hi"}),
        ToolCallFinished(id="0", tool_call=call, result="hi", is_error=False, fact_id=3),
        GenerationCompleted(),
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

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="missing", arguments={}),
        ToolCallFinished(id="0", tool_call=call, result="missing", is_error=True),
        GenerationCompleted(),
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


def test_tool_call_finished_fact_id_matches_facts_after_interleaved_events() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="earlier", arguments={}, result="r", is_error=False))
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.fact_id == facts(session)[-1].id


def test_failed_tool_call_has_no_fact_id() -> None:
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
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.fact_id is None


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
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "the answer"


def test_answer_citing_unknown_fact_continues_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
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
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [2]})
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

    events = [next(subscriber) for _ in range(8)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="delegate", arguments={"question": "what is x?"}),
        ToolCallFinished(
            id="0", tool_call=delegate_call, result="x is 1", is_error=False, fact_id=None
        ),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="done"),
        GenerationCompleted(),
        AssistantTextFinished(id="1"),
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


def test_each_llm_call_publishes_its_own_token_counts() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=call),
                GenerationComplete(
                    finish_reason="tool_calls", prompt_tokens=120, completion_tokens=17
                ),
            ],
            [
                TextDelta(text="done"),
                GenerationComplete(finish_reason="stop", prompt_tokens=160, completion_tokens=4),
            ],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(9)]
    completions = [event for event in events if isinstance(event, GenerationCompleted)]
    assert completions == [
        GenerationCompleted(prompt_tokens=120, completion_tokens=17),
        GenerationCompleted(prompt_tokens=160, completion_tokens=4),
    ]


def test_tool_error_is_recorded_as_error_and_excluded_from_facts() -> None:
    bus = Bus()
    session = _session()
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

    runner = LoopRunner(client, _failing_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert recorded[0].is_error is True
    assert "could not read /nope" in recorded[0].result
    assert facts(session) == []


def test_repeated_tool_errors_do_not_end_run_via_invalid_action_cap() -> None:
    bus = Bus()
    session = _session()
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

    runner = LoopRunner(client, _failing_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.invalid_action_attempts == 0
    errors = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.is_error
    ]
    assert len(errors) == MAX_INVALID_ACTION_ATTEMPTS
    assert list(session.events())[-1] == AssistantMessageRecorded(content="done", thinking="")


def test_shell_tool_call_publishes_result_deltas_before_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    call = ToolCall(id="1", name="shell", arguments={"command": "echo one; echo two"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break

    finished_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolCallFinished)
    )
    delta_indices = [
        index for index, event in enumerate(events) if isinstance(event, ToolCallResultDelta)
    ]
    assert delta_indices
    assert all(index < finished_index for index in delta_indices)
    finished = events[finished_index]
    assert isinstance(finished, ToolCallFinished)
    assert finished.result == "one\ntwo\n"


def test_model_recovers_a_truncated_fact_via_read_fact_and_cites_it() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="what is in the log?"))
    content = "needle " * 2000
    session.append(ToolCallRecorded(name="shell", arguments={}, result=content, is_error=False))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="read_fact", arguments={"id": 2})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="2",
                        name="answer",
                        arguments={"content": "it is needles", "citations": [2]},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 100, DEFAULT_LOOP_CONFIG)
    runner.execute()

    handle = client.seen_messages[0][-1]
    assert handle.tool_result is not None
    assert "fact 2 truncated" in handle.tool_result.content
    assert "call read_fact(2) for the full content" in handle.tool_result.content

    recalled = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    ]
    assert recalled[0].result == content
    assert recalled[0].is_error is False
    assert runner.final_answer == "it is needles"


def test_read_fact_with_unknown_id_is_recorded_as_error() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="read_fact", arguments={"id": 404})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert recorded[0].is_error is True
    assert "404" in recorded[0].result
    assert facts(session) == []
    assert runner.invalid_action_attempts == 0


def test_delegate_read_fact_cannot_reach_parent_facts() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="shell", arguments={}, result="parent secret", is_error=False)
    )
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="1", name="delegate", arguments={"question": "what is x?"}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(tool_call=ToolCall(id="c1", name="read_fact", arguments={"id": 2})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child = session.child("delegate/1")
    recalled = [
        event
        for event in child.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    ]
    assert recalled[0].is_error is True
    assert "parent secret" not in recalled[0].result


def _plan_messages(messages: list[Message]) -> list[Message]:
    return [
        message
        for message in messages
        if message.role is Role.USER and message.content.startswith("Your current plan:")
    ]


def test_no_plan_leaves_message_list_free_of_plan_messages() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")]])

    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_messages[0] == [
        Message(role=Role.SYSTEM, content=SYSTEM_PROMPT),
        Message(role=Role.USER, content="hi"),
    ]


def test_plan_message_tracks_completion_and_appears_exactly_once() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    registry = ToolRegistry()
    register_actions(registry, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="set_plan", arguments={"steps": ["one", "two"]})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="2", name="complete_step", arguments={"index": 0})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert _plan_messages(client.seen_messages[0]) == []
    assert [message.content for message in _plan_messages(client.seen_messages[1])] == [
        "Your current plan:\n[ ] 0. one\n[ ] 1. two\n"
        "Keep it current with set_plan and complete_step."
    ]
    assert [message.content for message in _plan_messages(client.seen_messages[2])] == [
        "Your current plan:\n[x] 0. one\n[ ] 1. two\n"
        "Keep it current with set_plan and complete_step."
    ]


def test_plan_message_is_never_persisted_to_the_session() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    registry = ToolRegistry()
    register_actions(registry, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="set_plan", arguments={"steps": ["one"]})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    events = list(session.events())
    assert PlanSet(steps=("one",)) in events
    assert not any(
        isinstance(event, UserMessageRecorded) and "Your current plan" in event.content
        for event in events
    )
    assert not any(isinstance(event, PlanStepCompleted) for event in events)


class RecordingClient:
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self.seen_messages: list[list[Message]] = []
        self.seen_specs: list[list[ToolSpec]] = []
        self.seen_ratios: list[float] = []
        self.runner: LoopRunner | None = None

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(messages)
        self.seen_specs.append(tools)
        if self.runner is not None:
            self.seen_ratios.append(self.runner.chars_per_token)
        yield from self._turns.pop(0)


def _sent_overhead(client: RecordingClient, index: int) -> int:
    messages = client.seen_messages[index]
    framing = [
        message
        for message in messages
        if message.role is Role.SYSTEM
        or (
            message.role is Role.USER
            and message.content.startswith(("Your current plan:", "you've repeated"))
        )
    ]
    text = specs_text(client.seen_specs[index]) + "".join(
        message_text(message) for message in framing
    )
    return estimate_tokens(text, DEFAULT_CHARS_PER_TOKEN)


def _context_size_for_budget(budget: int) -> int:
    size = budget
    while prompt_budget(size) < budget:
        size += 1
    return size


def _stop_turn() -> list[StreamEvent]:
    return [TextDelta(text="hello"), GenerationComplete(finish_reason="stop")]


def test_overhead_reflects_the_registered_tool_specs() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = RecordingClient([_stop_turn()])
    registry = _echo_registry()

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_specs[0] == registry.specs()
    assert _sent_overhead(client, 0) > estimate_tokens(SYSTEM_PROMPT)


def test_overhead_grows_when_a_plan_message_is_present() -> None:
    plain_session = _session()
    plain_session.append(UserMessageRecorded(content="hi"))
    plain_client = RecordingClient([_stop_turn()])
    LoopRunner(
        plain_client, _echo_registry(), Bus(), plain_session, 128_000, DEFAULT_LOOP_CONFIG
    ).execute()

    plan_session = _session("s2")
    plan_session.append(UserMessageRecorded(content="hi"))
    plan_session.append(PlanSet(steps=("one", "two")))
    plan_client = RecordingClient([_stop_turn()])
    LoopRunner(
        plan_client, _echo_registry(), Bus(), plan_session, 128_000, DEFAULT_LOOP_CONFIG
    ).execute()

    assert _sent_overhead(plan_client, 0) > _sent_overhead(plain_client, 0)


def test_overhead_grows_when_a_nudge_is_present() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(NUDGE_THRESHOLD)
    ]
    turns.append(_stop_turn())
    client = RecordingClient(turns)

    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_messages[-1][-1].role is Role.USER
    assert "repeated" in client.seen_messages[-1][-1].content
    assert _sent_overhead(client, len(client.seen_messages) - 1) > _sent_overhead(client, 0)


def test_overhead_shrinks_the_conversation_budget() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="a" * 3_000, is_error=False))
    registry = _echo_registry()
    overhead = estimate_tokens(
        specs_text(registry.specs()) + SYSTEM_PROMPT, DEFAULT_CHARS_PER_TOKEN
    )
    conversation = sum(
        estimate_tokens(message_text(message), DEFAULT_CHARS_PER_TOKEN)
        for message in session.messages()
    )
    context_size = _context_size_for_budget(conversation + overhead - 1)
    client = ScriptedClient([_stop_turn()])

    LoopRunner(client, registry, Bus(), session, context_size, DEFAULT_LOOP_CONFIG).execute()

    sent = next(m for m in client.seen_messages[0] if m.role is Role.TOOL)
    unaware = next(m for m in render_messages(session, context_size) if m.role is Role.TOOL)
    assert sent.tool_result is not None
    assert unaware.tool_result is not None
    assert "fact 2" in sent.tool_result.content
    assert unaware.tool_result.content == "a" * 3_000


def test_ratio_moves_toward_the_observed_value() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="x" * 3_000))
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="echo", arguments={"text": "hi"})),
                GenerationComplete(finish_reason="tool_calls", prompt_tokens=1_000),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert MIN_CHARS_PER_TOKEN <= runner.chars_per_token <= MAX_CHARS_PER_TOKEN
    assert runner.chars_per_token != DEFAULT_CHARS_PER_TOKEN


def test_ratio_clamps_at_the_lower_bound() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                TextDelta(text="done"),
                GenerationComplete(finish_reason="stop", prompt_tokens=1_000_000),
            ]
        ]
    )
    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert runner.chars_per_token == MIN_CHARS_PER_TOKEN


def test_ratio_clamps_at_the_upper_bound() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="x" * 10_000))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=1)]]
    )
    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert runner.chars_per_token == MAX_CHARS_PER_TOKEN


def test_missing_prompt_tokens_leaves_the_ratio_untouched() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([[TextDelta(text="done"), GenerationComplete(finish_reason="stop")]])
    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert runner.chars_per_token == DEFAULT_CHARS_PER_TOKEN


def test_second_call_estimates_with_the_updated_ratio() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="a" * 3_600, is_error=False))
    client = RecordingClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="echo", arguments={"text": "hi"})),
                GenerationComplete(finish_reason="tool_calls", prompt_tokens=1_000_000),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    runner = LoopRunner(client, _echo_registry(), Bus(), session, 2_400, DEFAULT_LOOP_CONFIG)
    client.runner = runner

    runner.execute()

    assert client.seen_ratios[0] == DEFAULT_CHARS_PER_TOKEN
    assert client.seen_ratios[1] == MIN_CHARS_PER_TOKEN
    first = [m for m in client.seen_messages[0] if m.role is Role.TOOL]
    second = [m for m in client.seen_messages[1] if m.role is Role.TOOL]
    assert first[0].tool_result is not None
    assert second[0].tool_result is not None
    assert first[0].tool_result.content == "a" * 3_600
    assert "fact 2" in second[0].tool_result.content


def test_overflowing_prompt_publishes_budget_exceeded() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=9_000)]]
    )

    LoopRunner(client, _echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG).execute()

    events = [next(subscriber) for _ in range(6)]
    exceeded = [event for event in events if isinstance(event, BudgetExceeded)]
    assert len(exceeded) == 1
    assert exceeded[0].actual == 9_000
    assert exceeded[0].budget == prompt_budget(8_192)
    assert exceeded[0].estimated < exceeded[0].actual


def test_fitting_prompt_publishes_no_budget_exceeded() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=100)]]
    )

    LoopRunner(client, _echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG).execute()

    events = [next(subscriber) for _ in range(5)]
    assert not any(isinstance(event, BudgetExceeded) for event in events)


def test_answer_with_passing_verification_ends_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1", name="answer", arguments={"content": "done", "citations": [], "verify": "true"}
    )
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "done"
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
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
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
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None

    target.write_text("hello")
    session = _session("s2")
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )
    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "wrote it"


def test_answer_without_verification_result_is_the_content_alone() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "done", "citations": []})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "done"
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.result == "done"
    assert recorded.is_error is False


def test_repeated_failing_verification_hits_stuckness_not_invalid_action_cap() -> None:
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    assert runner.invalid_action_attempts == 0
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(recorded) == STUCK_THRESHOLD
    assert all(event.is_error for event in recorded)


def test_answer_verification_timeout_is_a_tool_error_not_an_invalid_action() -> None:
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    with patch("pico.core.loop.Shell.run", side_effect=ToolError("command timed out after 30s")):
        runner.execute()

    assert runner.final_answer is None
    assert runner.invalid_action_attempts == 0
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is True
    assert "timed out" in recorded.result


def test_delegate_answering_with_verify_is_rejected() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            *[
                [
                    ToolCallReady(
                        tool_call=ToolCall(
                            id=f"c{index}",
                            name="answer",
                            arguments={
                                "content": "x is 1",
                                "citations": [],
                                "verify": "touch escaped.txt",
                            },
                        )
                    ),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
                for index in range(MAX_INVALID_ACTION_ATTEMPTS)
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_events = [
        event
        for event in session.child("delegate/1").events()
        if isinstance(event, ToolCallRecorded)
    ]
    assert all(event.is_error for event in child_events)
    assert "delegate may not use 'verify'" in child_events[0].result
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert parent[0].is_error is True
    assert "did not answer" in parent[0].result
