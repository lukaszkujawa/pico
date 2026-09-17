import itertools
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest.mock import patch

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions
from pico.core.bus import Bus
from pico.core.context import (
    PLAN_INLINE_HINT,
    PLAN_ORCHESTRATED_HINT,
    RECENT_UNITS,
    SYSTEM_PROMPT,
    compile_context,
    estimate_tokens,
    message_text,
    prompt_budget,
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
from pico.core.ledger import facts
from pico.core.loop import (
    BUDGET_WIND_DOWN_FRACTION,
    CONTEXT_PRESSURE_CAUSE,
    CROSSROADS_ACTIONS,
    DECISION_GRACE,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_LOOP_CONFIG,
    DEFAULT_LOOP_STEPS,
    MAX_ACTIONLESS_GENERATIONS,
    MAX_CHARS_PER_TOKEN,
    MAX_CROSSROADS,
    MAX_DELEGATE_STEPS,
    MAX_INVALID_ACTION_ATTEMPTS,
    MAX_RUN_STEPS,
    MAX_STEP_STEPS,
    MIN_CHARS_PER_TOKEN,
    RUNNER_ACTIONS,
    UNVERIFIED_PREFIX,
    LoopConfig,
    LoopRunner,
    StepOutcome,
    budget_step,
    decision_step,
    root_task,
    specs_text,
    step_orchestration_step,
    stream_step,
    stuckness_step,
    tool_call_step,
    vocabulary,
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
        self.seen_tools: list[list[ToolSpec]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(messages)
        self.seen_tools.append(tools)
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

    assert len(calls) == 4
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
            _answer_turn("hello world"),
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
        GenerationCompleted(),
        AssistantTextFinished(id="0"),
    ]
    assert runner.final_answer == "hello world"
    assert list(session.events())[:2] == [
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
            _answer_turn(),
            [
                ThinkingDelta(text="second thought"),
                TextDelta(text="second answer"),
                GenerationComplete(finish_reason="stop"),
            ],
            _answer_turn(),
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
        GenerationCompleted(),
        AssistantThinkingFinished(id="0"),
        AssistantTextFinished(id="1"),
    ]

    runner.final_answer = None
    runner.execute()

    second_events = [next(subscriber) for _ in range(12)]
    started_ids_second = [
        event.id
        for event in second_events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted)
    ]
    assert started_ids_second == ["3", "4"]

    assert [event for event in session.events() if isinstance(event, AssistantMessageRecorded)] == [
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
            _answer_turn(),
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(8)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="echo", arguments={"text": "hi"}),
        ToolCallFinished(id="0", tool_call=call, result="hi", is_error=False, fact_id=1),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="done"),
        GenerationCompleted(),
        AssistantTextFinished(id="1"),
    ]
    assert list(session.events())[:3] == [
        UserMessageRecorded(content="hi"),
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
            _answer_turn(),
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        ToolCallArgumentsDelta(id="0", name="echo", text='{"text":'),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="echo", arguments={"text": "hi"}),
        ToolCallFinished(id="0", tool_call=call, result="hi", is_error=False, fact_id=1),
        GenerationCompleted(),
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
            _answer_turn(),
        ]
    )

    runner = LoopRunner(client, ToolRegistry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="missing", arguments={}),
        ToolCallFinished(id="0", tool_call=call, result="missing", is_error=True),
        GenerationCompleted(),
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


def test_cancel_set_before_second_tool_call_leaves_it_unexecuted() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
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
        GenerationCompleted(),
        ToolCallStarted(id="0", name="first", arguments={}),
    ]
    finished = events[3]
    assert isinstance(finished, ToolCallFinished)
    assert finished.result == "first done"
    assert finished.is_error is False
    assert next(subscriber) == RunCancelled()
    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert [event.name for event in tool_events] == ["first"]


def test_default_loop_config_is_stuckness_budget_decision_steps_stream_then_tool_call() -> None:
    assert DEFAULT_LOOP_CONFIG.steps == (
        stuckness_step,
        budget_step,
        decision_step,
        step_orchestration_step,
        stream_step,
        tool_call_step,
    )
    assert DEFAULT_LOOP_CONFIG.max_steps == MAX_RUN_STEPS


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


def test_bookkeeping_tool_call_finishes_without_a_fact_id() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
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


def test_answer_citing_a_bookkeeping_call_seq_is_rejected() -> None:
    bus = Bus()
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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

    runner = LoopRunner(client, ToolRegistry(), bus, session, 1_000, DEFAULT_LOOP_CONFIG)
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
        [
            ToolCallReady(
                tool_call=ToolCall(id=str(i), name="note", arguments={"content": f"looking {i}"})
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(MAX_DELEGATE_STEPS)
    )
    client = ScriptedClient(turns)

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert tool_events[-1].name == "delegate"
    assert tool_events[-1].is_error is True


def test_delegate_can_run_shell_and_answer_with_verify(tmp_path: Path) -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    marker = tmp_path / "found.txt"
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
                        id="c1",
                        name="shell",
                        arguments={"command": f"echo 1 > {marker}"},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2",
                        name="answer",
                        arguments={
                            "content": "x is 1",
                            "citations": [],
                            "verify": f"test -f {marker}",
                        },
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_tool_events = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded)
    ]
    assert [event.name for event in child_tool_events] == ["shell", "answer"]
    assert all(not event.is_error for event in child_tool_events)
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert parent.name == "delegate"
    assert parent.result.startswith("x is 1")


def test_delegate_can_delegate_further_and_session_ids_nest() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "outer"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c1", name="delegate", arguments={"question": "inner"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="g1", name="answer", arguments={"content": "deep", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2", name="answer", arguments={"content": "outer done", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    grandchild = session.child("delegate/2").child("delegate/2")
    assert grandchild.session_id == "s1/delegate/2/delegate/2"
    assert [event for event in grandchild.events() if isinstance(event, UserMessageRecorded)] == [
        UserMessageRecorded(content="inner")
    ]
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert parent.result == "outer done"


def test_delegate_at_max_depth_is_not_offered_the_delegate_tool() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "deeper"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "no deeper", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    tools = ToolRegistry()
    register_actions(tools, session, depth=MAX_DELEGATE_DEPTH - 1)

    runner = LoopRunner(
        client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH - 1
    )
    runner.execute()

    child_specs = client.seen_tools[1]
    assert "delegate" not in {spec.name for spec in child_specs}
    assert "delegate" in {spec.name for spec in client.seen_tools[0]}


def test_cancelling_parent_mid_delegate_cancels_the_child_run() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    child_turn: list[StreamEvent] = [
        TextDelta(text="pondering"),
        GenerationComplete(finish_reason="stop"),
    ]

    class CancelDuringChild:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            self.calls += 1
            if self.calls == 1:
                yield ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                )
                yield GenerationComplete(finish_reason="tool_calls")
                return
            cancel.set()
            yield from child_turn

    runner = LoopRunner(
        CancelDuringChild(), _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
    runner.execute()

    child_events = list(session.child("delegate/2").events())
    assert [event for event in child_events if isinstance(event, AssistantMessageRecorded)] == []
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert parent == []


def test_repeating_delegate_is_stopped_by_stuckness() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    repeat: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="c", name="shell", arguments={"command": "false"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        *[list(repeat) for _ in range(MAX_DELEGATE_STEPS)],
        [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
    ]
    client = ScriptedClient(turns)
    tools = ToolRegistry()
    register_actions(tools, session)

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_calls = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded)
    ]
    assert len(child_calls) == STUCK_THRESHOLD + 1
    parent = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert parent.is_error is True
    assert "delegate failed" in parent.result
    assert "stuck" in parent.result


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

    child_session = session.child("delegate/2")
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

    repeats = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and not event.is_error
    ]
    assert len(repeats) == STUCK_THRESHOLD


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
                ToolCallReady(tool_call=ToolCall(id="1", name="read_fact", arguments={"id": 1})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="2",
                        name="answer",
                        arguments={"content": "it is needles", "citations": [1]},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 2000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    handle = client.seen_messages[0][-1]
    assert handle.tool_result is not None
    assert "fact 1 shell() truncated" in handle.tool_result.content
    assert "call read_fact(1) for the full content" in handle.tool_result.content

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

    child = session.child("delegate/3")
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
            [
                ToolCallReady(
                    tool_call=ToolCall(id="3", name="complete_step", arguments={"index": 1})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    ).execute()

    assert _plan_messages(client.seen_messages[0]) == []
    [after_set] = _plan_messages(client.seen_messages[1])
    assert after_set.content.startswith(
        "Your current plan:\n[ ] 0. one\n[ ] 1. two\n"
        "Keep it current with set_plan and complete_step."
    )
    [after_complete] = _plan_messages(client.seen_messages[2])
    assert after_complete.content.startswith(
        "Your current plan:\n[x] 0. one\n[ ] 1. two\n"
        "Keep it current with set_plan and complete_step."
    )


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
            _answer_turn(),
        ]
    )

    LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    ).execute()

    events = list(session.events())
    assert PlanSet(steps=("one",)) in events
    assert not any(
        isinstance(event, UserMessageRecorded) and "Your current plan" in event.content
        for event in events
    )
    assert not any(isinstance(event, PlanStepCompleted) for event in events)


def test_stream_step_sends_briefing_and_window_not_the_full_transcript() -> None:
    session = _session()
    session.append(PlanSet(steps=("keep going",)))
    session.append(PlanStepCompleted(index=0))
    session.append(UserMessageRecorded(content="start"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="noted", is_error=False))
    for index in range(RECENT_UNITS + 4):
        session.append(UserMessageRecorded(content=f"step {index}"))
        session.append(AssistantMessageRecorded(content=f"done {index}", thinking=""))
    client = ScriptedClient([_answer_turn()])
    transcript = session.messages()

    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    sent = client.seen_messages[0]
    assert sent[0].role is Role.SYSTEM
    briefing = sent[1]
    assert briefing.content.startswith("Your current plan:")
    assert "Facts gathered so far:" in briefing.content
    window = sent[2:-1]
    assert len(window) < len(transcript)
    assert window == [transcript[0], *transcript[-(RECENT_UNITS + 1) :]]
    assert sent[-1].content.startswith("decision required")
    assert sum("Your current plan:" in message.content for message in sent) == 1


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


def _answer_turn(content: str = "done") -> list[StreamEvent]:
    return [
        ToolCallReady(
            tool_call=ToolCall(
                id="a1", name="answer", arguments={"content": content, "citations": []}
            )
        ),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def test_overhead_reflects_the_registered_tool_specs() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = RecordingClient([_stop_turn()])
    registry = _echo_registry()

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_specs[0] == vocabulary(registry, 0)
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
    plan_client = RecordingClient([_answer_turn()])
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
        specs_text(vocabulary(registry, 0)) + SYSTEM_PROMPT, DEFAULT_CHARS_PER_TOKEN
    )
    conversation = sum(
        estimate_tokens(message_text(message), DEFAULT_CHARS_PER_TOKEN)
        for message in session.messages()
    )
    briefing = estimate_tokens(
        message_text(compile_context(session, 128_000)[0]), DEFAULT_CHARS_PER_TOKEN
    )
    context_size = _context_size_for_budget(conversation + briefing + overhead - 1)
    client = ScriptedClient([_stop_turn()])

    LoopRunner(client, registry, Bus(), session, context_size, DEFAULT_LOOP_CONFIG).execute()

    sent = next(m for m in client.seen_messages[0] if m.role is Role.TOOL)
    unaware = next(m for m in compile_context(session, context_size) if m.role is Role.TOOL)
    assert sent.tool_result is not None
    assert unaware.tool_result is not None
    assert "fact 1" in sent.tool_result.content
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
    assert "fact 1" in second[0].tool_result.content


def test_pinned_overflow_publishes_budget_exceeded_before_the_request() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="h" * 40_000))
    client = ScriptedClient(
        [
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=9_000)],
            _answer_turn(),
        ]
    )

    LoopRunner(client, _echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG).execute()

    events: list[BusEvent] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    exceeded = [event for event in events if isinstance(event, BudgetExceeded)]
    assert exceeded
    assert exceeded[0].budget == prompt_budget(8_192)
    assert exceeded[0].estimated > exceeded[0].budget
    generation = next(i for i, event in enumerate(events) if isinstance(event, GenerationCompleted))
    assert events.index(exceeded[0]) < generation


def test_reported_overflow_alone_publishes_no_budget_exceeded() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=9_000)]]
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert not any(isinstance(event, BudgetExceeded) for event in events)
    assert runner.chars_per_token == MIN_CHARS_PER_TOKEN


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
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(recorded) == STUCK_THRESHOLD + 1
    assert all(event.is_error for event in recorded)
    assert runner.error is not None
    assert "stuck" in runner.error


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


def _delegate_run(
    fields: object, answers: list[str], question: str = "how many?"
) -> tuple[Session, ScriptedClient]:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    arguments: dict[str, object] = {"question": question}
    if fields is not None:
        arguments["fields"] = fields
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id="1", name="delegate", arguments=arguments)),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        *[
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id=f"c{index}",
                        name="answer",
                        arguments={"content": content, "citations": []},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ]
            for index, content in enumerate(answers)
        ],
        _answer_turn(),
    ]
    client = ScriptedClient(turns)
    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()
    return session, client


def _parent_delegate_result(session: Session) -> ToolCallRecorded:
    return next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "delegate"
    )


def _child_answers(session: Session) -> list[ToolCallRecorded]:
    return [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded)
    ]


def test_typed_delegate_returns_conforming_json_verbatim() -> None:
    record = '{"count": 3, "unit": "files"}'
    session, client = _delegate_run({"count": "number", "unit": "string"}, [record])

    result = _parent_delegate_result(session)
    assert result.is_error is False
    assert result.result == record
    child_question = next(
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, UserMessageRecorded)
    )
    assert '"count": <number>' in child_question.content
    assert child_question.content.startswith("how many?")
    assert len(client.seen_messages) == 3


def test_typed_delegate_rejects_non_json_answer_and_child_retries() -> None:
    session, _ = _delegate_run({"count": "number"}, ["three", '{"count": 3}'])

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "did not parse as JSON" in answers[0].result
    assert answers[1].is_error is False
    assert _parent_delegate_result(session).result == '{"count": 3}'


def test_typed_delegate_rejects_missing_field_and_child_retries() -> None:
    session, _ = _delegate_run(
        {"count": "number", "unit": "string"},
        ['{"count": 3}', '{"count": 3, "unit": "files"}'],
    )

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "missing required field(s): ['unit']" in answers[0].result
    assert answers[1].is_error is False


def test_typed_delegate_rejects_extra_field_and_child_retries() -> None:
    session, _ = _delegate_run({"count": "number"}, ['{"count": 3, "extra": 1}', '{"count": 3}'])

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "unexpected field(s): ['extra']" in answers[0].result
    assert answers[1].is_error is False


def test_typed_delegate_rejects_wrong_typed_field_and_child_retries() -> None:
    session, _ = _delegate_run({"count": "number"}, ['{"count": "3"}', '{"count": 3}'])

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "field 'count' must be a number, got str" in answers[0].result
    assert answers[1].is_error is False


def test_untyped_delegate_passes_prose_answer_unchanged() -> None:
    session, _ = _delegate_run(None, ["there are three files"], question="how many?")

    result = _parent_delegate_result(session)
    assert result.is_error is False
    assert result.result == "there are three files"
    child_question = next(
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, UserMessageRecorded)
    )
    assert child_question.content == "how many?"


def test_invalid_delegate_fields_is_an_invalid_action_on_the_parent() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id=f"{index}",
                    name="delegate",
                    arguments={"question": "q", "fields": {"when": "date"}},
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for index in range(MAX_INVALID_ACTION_ATTEMPTS)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert all(event.is_error for event in recorded)
    assert "unknown type 'date'" in recorded[0].result
    assert runner.invalid_action_attempts == MAX_INVALID_ACTION_ATTEMPTS
    assert list(session.child("delegate/2").events()) == []


def test_non_object_delegate_fields_is_an_invalid_action_on_the_parent() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="1",
                        name="delegate",
                        arguments={"question": "q", "fields": ["count"]},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert "must be an object" in recorded.result


def _delegate_turn(question: str = "q") -> list[list[StreamEvent]]:
    return [
        [
            ToolCallReady(
                tool_call=ToolCall(id="1", name="delegate", arguments={"question": question})
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id="c1", name="answer", arguments={"content": "done", "citations": []}
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        [TextDelta(text="ok"), GenerationComplete(finish_reason="stop")],
    ]


def test_delegates_across_turns_get_distinct_child_sessions() -> None:
    session = _session()

    session.append(UserMessageRecorded(content="first"))
    client = ScriptedClient(_delegate_turn("first question"))
    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    session.append(UserMessageRecorded(content="second"))
    client = ScriptedClient(_delegate_turn("second question"))
    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    first_child_questions = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, UserMessageRecorded)
    ]
    second_child_questions = [
        event
        for event in session.child("delegate/5").events()
        if isinstance(event, UserMessageRecorded)
    ]
    assert first_child_questions == [UserMessageRecorded(content="first question")]
    assert second_child_questions == [UserMessageRecorded(content="second question")]


def test_delegate_call_at_max_depth_is_rejected_without_spawning_a_child() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    tools = ToolRegistry()
    register_actions(tools, session, depth=MAX_DELEGATE_DEPTH)

    runner = LoopRunner(
        client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert runner.invalid_action_attempts == 1
    assert list(session.child("delegate/2").events()) == []


def test_invalid_registry_tool_arguments_are_recorded_as_errors_not_facts() -> None:
    session = _session()
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
    assert runner.invalid_action_attempts == 1
    assert facts(session) == []


def test_unexpected_tool_exception_finishes_run_with_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
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
    assert runner.error == "wired wrong"
    assert events[-2] == ErrorOccurred(message="wired wrong")
    assert events[-1] == RunFinished(error="wired wrong")


def test_invalid_action_streak_resets_on_a_valid_call() -> None:
    session = _session()
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

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(recorded) == 2 * (MAX_INVALID_ACTION_ATTEMPTS - 1) + 1
    assert runner.invalid_action_attempts == MAX_INVALID_ACTION_ATTEMPTS - 1


def _drain_until_run_finished(subscriber: Iterator[BusEvent]) -> list[BusEvent]:
    events: list[BusEvent] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            return events
    return events


def _set_plan_turn(steps: list[str]) -> list[StreamEvent]:
    return [
        ToolCallReady(tool_call=ToolCall(id="p1", name="set_plan", arguments={"steps": steps})),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def _text_turn(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text), GenerationComplete(finish_reason="stop")]


def test_narration_without_a_plan_demands_a_decision_instead_of_ending_the_run() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([_text_turn("just chatting"), _answer_turn("here it is")])

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.error is None
    assert runner.final_answer == "here it is"
    demand = client.seen_messages[1][-1]
    assert demand.role is Role.USER
    assert demand.content.startswith("decision required")
    assert "set_plan" in demand.content
    assert "answer" in demand.content
    assert "why" in demand.content


def test_narration_with_unfinished_plan_is_nudged_and_run_continues() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    registry = ToolRegistry()
    register_actions(registry, session)
    client = ScriptedClient(
        [
            _set_plan_turn(["review the code"]),
            _text_turn("now let me look at the next file"),
            _answer_turn("all reviewed"),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.final_answer == "all reviewed"
    assert runner.error is None
    nudge = client.seen_messages[2][-1]
    assert nudge.role is Role.USER
    assert "took no action" in nudge.content


def test_repeated_narration_with_unfinished_plan_stops_the_run_with_an_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    registry = ToolRegistry()
    register_actions(registry, session)
    turns = [
        _set_plan_turn(["review the code"]),
        *[_text_turn(f"musing {i}") for i in range(MAX_ACTIONLESS_GENERATIONS)],
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(
        client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.final_answer is None
    assert runner.error is not None
    assert "without a tool call" in runner.error
    assert _drain_until_run_finished(subscriber)[-1] == RunFinished(error=runner.error)


def test_stuck_run_carries_its_reason_on_run_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(STUCK_THRESHOLD)
    ]
    turns.append(_text_turn("I have nothing to add"))
    runner = LoopRunner(
        ScriptedClient(turns), _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG
    )
    runner.execute()

    assert runner.error is not None
    assert "stuck" in runner.error
    events = _drain_until_run_finished(subscriber)
    assert ErrorOccurred(message=runner.error) in events
    assert events[-1] == RunFinished(error=runner.error)


def test_delegate_at_max_depth_reports_it_is_unavailable() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            _text_turn("done"),
        ]
    )

    runner = LoopRunner(
        client,
        _echo_registry(),
        Bus(),
        session,
        128_000,
        DEFAULT_LOOP_CONFIG,
        depth=MAX_DELEGATE_DEPTH,
    )
    runner.execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert "not available at this depth" in recorded.result
    assert runner.invalid_action_attempts == 1


def test_long_single_prompt_run_keeps_the_prompt_bounded() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="review everything"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id=str(i), name="echo", arguments={"text": f"chunk {i} " + "x" * 1500}
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(25)
    ]
    turns.append([TextDelta(text="done"), GenerationComplete(finish_reason="stop")])
    client = ScriptedClient(turns)

    LoopRunner(client, _echo_registry(), Bus(), session, 4000, DEFAULT_LOOP_CONFIG).execute()

    final = sum(len(message_text(message)) for message in client.seen_messages[-1])
    full_transcript = sum(len(message_text(message)) for message in session.messages())
    assert final < full_transcript / 2
    assert final <= prompt_budget(4000) * 6
    task = client.seen_messages[-1][2]
    assert task.role is Role.USER
    assert task.content == "review everything"


def _search_call(query: str = "review findings architecture core loop") -> ToolCall:
    return ToolCall(id="1", name="search_facts", arguments={"query": query})


class SearchScriptedClient:
    def __init__(self, turns: list[list[StreamEvent]], replies: list[str]) -> None:
        self._turns = turns
        self._replies = replies
        self.seen_tools: list[list[ToolSpec]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_tools.append(tools)
        if not tools:
            yield TextDelta(text=self._replies.pop(0))
            return
        yield from self._turns.pop(0)


def _seeded_session() -> Session:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(
            name="note", arguments={}, result="the run loop compiles context", is_error=False
        )
    )
    return session


def test_search_facts_returns_ids_with_reasons_from_the_sub_task() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(tool_call=_search_call()),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        ["1", "1", "[1] the note records how the core loop builds its prompt"],
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    recorded = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    ]
    assert recorded[0].is_error is False
    assert recorded[0].result.startswith("[1] the note records how the core loop builds its prompt")
    assert client.seen_tools[1] == []


def test_search_facts_inside_a_delegate_sends_no_tool_specs() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c1", name="note", arguments={"content": "seed"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c2", name="search_facts", arguments={"query": "seed"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c3", name="answer", arguments={"content": "ok", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        ["1", "1", "[1] seed is the match"],
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    child = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    ]
    assert child[0].result.startswith("[1] seed is the match")
    assert [] in client.seen_tools


def test_search_facts_without_relevant_facts_offers_the_fact_index() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(tool_call=_search_call()),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        ["none"],
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    )
    assert recorded.is_error is False
    assert "no relevant facts found for" in recorded.result
    assert "[1] note(): the run loop compiles context" in recorded.result


def test_search_facts_with_empty_query_is_an_invalid_action() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(tool_call=_search_call("   ")),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        [],
    )

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    )
    assert recorded.is_error is True
    assert runner.invalid_action_attempts == 1


def test_llm_error_mid_search_records_a_failed_call_and_the_run_continues() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)

    class ExplodingSearch:
        def __init__(self) -> None:
            self.turns = [
                [
                    ToolCallReady(tool_call=_search_call()),
                    GenerationComplete(finish_reason="tool_calls"),
                ],
                _answer_turn(),
            ]

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            if not tools:
                raise LLMError("connection lost")
            yield from self.turns.pop(0)

    runner = LoopRunner(ExplodingSearch(), tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    )
    assert recorded.is_error is True
    assert "search failed" in recorded.result
    assert runner.error is None
    assert runner.iterations == 2


def test_cancelling_mid_search_cancels_the_run_without_recording_a_result() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    cancel = threading.Event()

    class CancelDuringSearch:
        def __init__(self) -> None:
            self.turns = [
                [
                    ToolCallReady(tool_call=_search_call()),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
            ]

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            if not tools:
                cancel.set()
                yield TextDelta(text="2")
                return
            yield from self.turns.pop(0)

    bus = Bus()
    subscriber = bus.subscribe()
    runner = LoopRunner(
        CancelDuringSearch(), tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
    runner.execute()

    published = [next(subscriber) for _ in range(4)]
    assert RunCancelled() in published
    assert [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    ] == []


def test_vocabulary_lists_plain_tools_then_runner_actions() -> None:
    registry = ToolRegistry()
    register_actions(registry, _session())

    specs = vocabulary(registry, 0)

    assert [spec.name for spec in specs] == [
        "read_file",
        "write_file",
        "load_table",
        "sql",
        "note",
        "read_fact",
        "set_plan",
        "complete_step",
        "shell",
        "answer",
        "search_facts",
        "delegate",
    ]
    assert specs[-4:] == [action.spec for action in RUNNER_ACTIONS.values()]


def test_vocabulary_omits_delegate_at_max_depth() -> None:
    registry = ToolRegistry()
    register_actions(registry, _session(), depth=MAX_DELEGATE_DEPTH)

    names = [spec.name for spec in vocabulary(registry, MAX_DELEGATE_DEPTH)]

    assert "delegate" not in names
    assert "shell" in names
    assert names == [spec.name for spec in vocabulary(registry, 0) if spec.name != "delegate"]


def test_answer_spec_documents_verify() -> None:
    spec = RUNNER_ACTIONS["answer"].spec
    properties = spec.parameters["properties"]
    required = spec.parameters["required"]
    assert isinstance(properties, dict)
    assert isinstance(required, list)
    assert "verify" in properties
    assert "verify" not in required
    assert "exits 0" in spec.description


def test_budget_step_is_noop_when_max_steps_is_none() -> None:
    runner = LoopRunner(
        FailingClient(), _echo_registry(), Bus(), _session(), 128_000, LoopConfig(steps=())
    )
    runner.iterations = 1_000_000
    assert budget_step(runner) == "continue"
    assert runner.pending_nudge is None
    assert runner.error is None


def test_budget_step_is_noop_for_delegates_regardless_of_iterations() -> None:
    runner = LoopRunner(
        FailingClient(),
        _echo_registry(),
        Bus(),
        _session(),
        128_000,
        LoopConfig(steps=(), max_steps=MAX_DELEGATE_STEPS),
        depth=1,
    )
    runner.iterations = MAX_DELEGATE_STEPS
    assert budget_step(runner) == "continue"
    assert runner.pending_nudge is None
    assert runner.error is None


def test_budget_step_below_wind_down_threshold_sets_no_nudge() -> None:
    max_steps = 10
    runner = LoopRunner(
        FailingClient(),
        _echo_registry(),
        Bus(),
        _session(),
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = int(max_steps * BUDGET_WIND_DOWN_FRACTION) - 1
    assert budget_step(runner) == "continue"
    assert runner.pending_nudge is None


def test_budget_step_at_wind_down_threshold_sets_nudge_with_remaining_count() -> None:
    max_steps = 10
    runner = LoopRunner(
        FailingClient(),
        _echo_registry(),
        Bus(),
        _session(),
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    outcome = budget_step(runner)
    remaining = max_steps - runner.iterations
    assert outcome == "continue"
    assert runner.pending_nudge is not None
    assert str(remaining) in runner.pending_nudge
    assert "answer" in runner.pending_nudge


def test_budget_step_leaves_the_kill_to_the_last_words_generation() -> None:
    max_steps = 10
    runner = LoopRunner(
        FailingClient(),
        _echo_registry(),
        Bus(),
        _session(),
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = max_steps

    assert budget_step(runner) == "continue"
    assert runner.error is None


def test_run_reaching_soft_threshold_gets_wind_down_nudge_then_answers_normally() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 5
    threshold_iteration = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"i": i})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(threshold_iteration - 1)
    ]
    turns.append(
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id="answer", name="answer", arguments={"content": "done", "citations": []}
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
    )
    client = ScriptedClient(turns)
    config = LoopConfig(
        steps=(stuckness_step, budget_step, stream_step, tool_call_step), max_steps=max_steps
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, config)
    runner.execute()

    last_messages = client.seen_messages[-1]
    nudges = [m for m in last_messages if m.role is Role.USER and "generation budget" in m.content]
    assert len(nudges) == 1
    assert runner.final_answer == "done"
    assert runner.error is None


def test_run_exhausting_budget_fails_explicitly_and_stays_resumable() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 3
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"i": i})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(max_steps)
    ]
    turns.append(_text_turn("I have nothing to say"))
    client = ScriptedClient(turns)
    config = LoopConfig(
        steps=(stuckness_step, budget_step, stream_step, tool_call_step), max_steps=max_steps
    )

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, config)
    runner.execute()

    expected = f"run stopped: the generation budget of {max_steps} is spent"
    assert runner.error == expected
    last_event = next(subscriber)
    while not isinstance(last_event, RunFinished):
        last_event = next(subscriber)
    assert last_event == RunFinished(error=expected)

    session.append(UserMessageRecorded(content="continue please"))
    followup_client = ScriptedClient(
        [
            [TextDelta(text="picking up"), GenerationComplete(finish_reason="stop")],
            _answer_turn("picked up"),
        ]
    )
    followup_runner = LoopRunner(followup_client, _echo_registry(), Bus(), session, 128_000, config)
    followup_runner.execute()

    assert followup_runner.error is None
    assert followup_runner.final_answer == "picked up"
    echoes = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "echo"
    ]
    assert len(echoes) == max_steps


def test_delegate_that_only_narrates_returns_its_last_narration_marked_unverified() -> None:
    bus = Bus()
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    turns: list[list[StreamEvent]] = [
        [ToolCallReady(tool_call=delegate_call), GenerationComplete(finish_reason="tool_calls")],
    ]
    turns.extend(
        [TextDelta(text=f"thinking {index}"), GenerationComplete(finish_reason="stop")]
        for index in range(MAX_DELEGATE_STEPS)
    )
    turns.append(_answer_turn())
    client = ScriptedClient(turns)

    runner = LoopRunner(client, _echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    delegated = _parent_delegate_result(session)
    assert delegated.is_error is False
    assert delegated.result.startswith(UNVERIFIED_PREFIX)
    assert delegated.result.endswith("thinking 5")
    assert runner.final_answer == "done"


def test_parent_can_read_and_cite_a_fact_minted_inside_a_delegate() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="note", arguments={"content": "the port is 8421"}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2", name="answer", arguments={"content": "done", "citations": [1]}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(tool_call=ToolCall(id="2", name="read_fact", arguments={"id": 1})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="3", name="answer", arguments={"content": "8421", "citations": [1]}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recalled = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    )
    assert recalled.result == "the port is 8421"
    assert recalled.is_error is False
    assert runner.final_answer == "8421"


def test_child_sees_parent_facts_in_its_briefing_index() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="note", arguments={}, result="the port is 8421", is_error=False)
    )
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "ok", "citations": [1]}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    briefing = client.seen_messages[1][1]
    assert "[1] note(): the port is 8421" in briefing.content


def _step_children(session: Session) -> list[Session]:
    rows = session.connection.execute(
        "SELECT DISTINCT session_id FROM events "
        "WHERE session_id LIKE ? AND session_id NOT LIKE ? ORDER BY id",
        (f"{session.session_id}/step/%", f"{session.session_id}/step/%/%"),
    ).fetchall()
    return [Session(session.connection, str(row[0])) for row in rows]


def _step_session() -> tuple[Session, ToolRegistry]:
    session = _session()
    session.append(UserMessageRecorded(content="survey the repository"))
    registry = ToolRegistry()
    register_actions(registry, session)
    return session, registry


def test_each_plan_step_runs_in_its_own_child_session() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files", "name the largest"]),
            _answer_turn("there are 12 files"),
            _text_turn("good"),
            _answer_turn("loop.py is largest"),
            _answer_turn("12 files, loop.py is largest"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "12 files, loop.py is largest"
    first, second = _step_children(session)
    assert first.session_id != second.session_id
    assert list(first.events()) != list(second.events())
    assert "count the files" in root_task(first)
    assert "name the largest" in root_task(second)


def test_second_step_prompt_carries_the_first_steps_result_and_plan_progress() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files", "name the largest"]),
            _answer_turn("there are 12 files"),
            _text_turn("good"),
            _answer_turn("loop.py is largest"),
            _answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    second = root_task(_step_children(session)[1])
    assert "survey the repository" in second
    assert "[x] 0. count the files" in second
    assert "[ ] 1. name the largest" in second
    assert "there are 12 files" in second


def test_accepted_child_answer_settles_the_step_without_complete_step() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [_set_plan_turn(["count the files"]), _answer_turn("there are 12 files"), _answer_turn()]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert PlanStepCompleted(index=0) in list(session.events())
    assert not any(
        isinstance(event, ToolCallRecorded) and event.name == "complete_step"
        for event in session.events()
    )
    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.result == "there are 12 files"
    assert recorded.is_error is False


def test_step_result_is_a_fact_recoverable_from_anywhere_in_the_tree() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [_set_plan_turn(["count the files"]), _answer_turn("there are 12 files"), _answer_turn()]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    step_facts = [fact for fact in facts(session) if fact.source == "step"]
    assert [fact.content for fact in step_facts] == ["there are 12 files"]
    assert facts(_step_children(session)[0]) == facts(session)


def test_settled_step_is_not_re_run_when_the_parent_does_nothing() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            _answer_turn("there are 12 files"),
            _text_turn("thinking"),
            _answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    steps = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    ]
    assert len(steps) == 1


def test_revising_the_plan_changes_which_step_runs_next() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            _answer_turn("there are 12 files"),
            _set_plan_turn(["count the files", "name the largest"]),
            _answer_turn("loop.py is largest"),
            _answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    steps = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    ]
    assert [event.arguments["step"] for event in steps] == ["count the files", "count the files"]
    assert [event.result for event in steps] == ["there are 12 files", "loop.py is largest"]


def test_failed_step_is_retried_once_then_fails_the_node() -> None:
    session, registry = _step_session()

    class PlanThenFailingChildren:
        def __init__(self) -> None:
            self.turns = [_set_plan_turn(["count the files"])]

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            if any("Your step is step" in message.content for message in messages):
                raise LLMError("connection lost")
            yield from (self.turns.pop(0) if self.turns else _text_turn("waiting"))

    runner = LoopRunner(
        PlanThenFailingChildren(), registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG
    )
    runner.execute()

    steps = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    ]
    assert len(steps) == 2
    assert all(event.is_error for event in steps)
    assert runner.error is not None
    assert "count the files" in runner.error
    assert not any(isinstance(event, PlanStepCompleted) for event in session.events())


def test_parent_context_after_a_step_holds_the_result_but_no_child_transcript() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c1", name="note", arguments={"content": "private"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            _answer_turn("there are 12 files"),
            _answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    parent_window = "".join(message_text(message) for message in client.seen_messages[-1][2:])
    assert "there are 12 files" in parent_window
    assert "private" not in parent_window


def test_a_step_child_may_plan_and_recurse_into_its_own_children() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["survey the code"]),
            _set_plan_turn(["read loop.py"]),
            _answer_turn("loop.py drives the run"),
            _answer_turn("the survey is done"),
            _answer_turn("all done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    [child] = _step_children(session)
    [grandchild] = _step_children(child)
    assert "read loop.py" in root_task(grandchild)
    assert runner.final_answer == "all done"


def test_plan_at_max_depth_runs_inline_with_no_child_spawned() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            [
                ToolCallReady(
                    tool_call=ToolCall(id="2", name="complete_step", arguments={"index": 0})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            _answer_turn("done"),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.final_answer == "done"
    assert PlanStepCompleted(index=0) in list(session.events())
    assert not any(
        isinstance(event, ToolCallRecorded) and event.name == "step" for event in session.events()
    )


def test_a_run_without_a_plan_spawns_no_step_children() -> None:
    session, registry = _step_session()
    client = ScriptedClient([_answer_turn("nothing to plan")])

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "nothing to plan"
    assert not any(
        isinstance(event, ToolCallRecorded) and event.name == "step" for event in session.events()
    )


def test_set_plan_description_names_fresh_agents_and_self_contained_steps() -> None:
    registry = ToolRegistry()
    register_actions(registry, _session())

    description = next(spec for spec in registry.specs() if spec.name == "set_plan").description

    assert "fresh agent" in description
    assert "stand alone" in description


def test_orchestrated_briefing_replaces_the_complete_step_hint() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [_set_plan_turn(["count the files"]), _answer_turn("12"), _answer_turn("done")]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    briefing = client.seen_messages[-1][1]
    assert PLAN_ORCHESTRATED_HINT in briefing.content
    assert PLAN_INLINE_HINT not in briefing.content


def _repeat_turns(count: int) -> list[list[StreamEvent]]:
    return [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(count)
    ]


def test_budget_exhaustion_forces_a_final_answer_that_becomes_the_result() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 3
    turns = [*_repeat_turns(max_steps), _answer_turn("what I found so far")]
    client = ScriptedClient(turns)
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.final_answer == "what I found so far"
    assert runner.error is None
    nudge = client.seen_messages[-1][-1]
    assert nudge.role is Role.USER
    assert "final generation" in nudge.content
    assert "budget" in nudge.content


def test_the_final_generation_offers_only_the_answer_tool() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    client = ScriptedClient([*_repeat_turns(max_steps), _answer_turn("all I have")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, config).execute()

    assert [spec.name for spec in client.seen_tools[-1]] == ["answer"]
    assert len(client.seen_tools[0]) > 1


def test_a_stuck_node_answers_before_the_stuck_failure() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    turns = [*_repeat_turns(STUCK_THRESHOLD), _answer_turn("partial findings")]

    runner = LoopRunner(
        ScriptedClient(turns), _echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG
    )
    runner.execute()

    assert runner.final_answer == "partial findings"
    assert runner.error is None


def test_a_non_answer_call_in_the_final_generation_keeps_the_original_failure() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    turns = [*_repeat_turns(max_steps + 1)]
    client = ScriptedClient(turns)
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.final_answer is None
    assert runner.error == f"run stopped: the generation budget of {max_steps} is spent"
    rejected = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert rejected.is_error is True
    assert "answer is the only tool left" in rejected.result


def test_silence_in_the_final_generation_fails_with_the_original_error() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    client = ScriptedClient([*_repeat_turns(max_steps), _text_turn("no comment")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, _echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.final_answer is None
    assert runner.error == f"run stopped: the generation budget of {max_steps} is spent"


def test_a_step_child_that_dies_returns_a_partial_answer_marked_partial() -> None:
    session, registry = _step_session()
    repeat: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="c", name="shell", arguments={"command": "true"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            *[list(repeat) for _ in range(STUCK_THRESHOLD)],
            _answer_turn("I counted 7 before running out"),
            _answer_turn("done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is False
    assert recorded.result.startswith("partial — you are stuck")
    assert "I counted 7 before running out" in recorded.result
    assert PlanStepCompleted(index=0) in list(session.events())
    assert runner.final_answer == "done"


def test_the_wind_down_nudge_still_fires_before_the_final_generation() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 5
    client = ScriptedClient([*_repeat_turns(max_steps), _answer_turn("wrapping up")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, _echo_registry(), Bus(), session, 128_000, config).execute()

    nudges = [
        message.content
        for messages in client.seen_messages
        for message in messages[-1:]
        if message.role is Role.USER and "generation budget" in message.content
    ]
    assert any("generations remain" in nudge for nudge in nudges)
    assert "final generation" in client.seen_messages[-1][-1].content


def test_a_step_child_that_ignores_two_crossroads_answers_partially() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            *[
                [
                    ToolCallReady(
                        tool_call=ToolCall(
                            id="c", name="shell", arguments={"command": f"echo {index}"}
                        )
                    ),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
                for index in range(MAX_STEP_STEPS)
            ],
            _answer_turn("done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is True
    assert f"{MAX_CROSSROADS} decision points passed" in recorded.result


def test_a_step_child_answering_at_its_crossroads_settles_the_step() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["count the files"]),
            *[
                [
                    ToolCallReady(
                        tool_call=ToolCall(
                            id="c", name="shell", arguments={"command": f"echo {index}"}
                        )
                    ),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
                for index in range(RECENT_UNITS + DECISION_GRACE + 2)
            ],
            _answer_turn("I counted 7 files"),
            _answer_turn("done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is False
    assert recorded.result == "I counted 7 files"
    assert runner.final_answer == "done"


def _note_turn(index: int) -> list[StreamEvent]:
    return [
        ToolCallReady(
            tool_call=ToolCall(
                id=str(index), name="note", arguments={"content": f"finding {index}"}
            )
        ),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def _decision_session() -> tuple[Session, ToolRegistry]:
    session = _session()
    session.append(UserMessageRecorded(content="survey the repository"))
    registry = ToolRegistry()
    register_actions(registry, session, depth=MAX_DELEGATE_DEPTH)
    return session, registry


def _demands(client: ScriptedClient) -> list[str]:
    return [
        messages[-1].content
        for messages in client.seen_messages
        if messages[-1].role is Role.USER and messages[-1].content.startswith("decision required")
    ]


def test_transcript_past_the_structural_bound_demands_a_decision_once() -> None:
    session, registry = _decision_session()
    client = ScriptedClient(
        [*[_note_turn(index) for index in range(RECENT_UNITS + 3)], _answer_turn()]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    demands = _demands(client)
    assert len(demands) == 1
    assert CONTEXT_PRESSURE_CAUSE in demands[0]
    assert "set_plan" in demands[0] and "answer" in demands[0] and "why" in demands[0]


def test_a_node_with_an_active_plan_never_sees_the_demand() -> None:
    session, registry = _decision_session()
    client = ScriptedClient(
        [
            _set_plan_turn(["keep counting"]),
            *[_note_turn(index) for index in range(RECENT_UNITS + 3)],
            _answer_turn(),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert _demands(client) == []


def test_the_root_at_wind_down_with_no_plan_gets_the_demand_not_the_wind_down_nudge() -> None:
    session, registry = _decision_session()
    max_steps = 5
    threshold = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    client = ScriptedClient([*[_note_turn(index) for index in range(threshold)], _answer_turn()])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, registry, Bus(), session, 128_000, config).execute()

    demands = _demands(client)
    assert len(demands) == 1
    assert "generations remain of your budget" in demands[0]


def test_the_root_at_wind_down_with_unfinished_steps_keeps_the_wind_down_wording() -> None:
    session, registry = _decision_session()
    max_steps = 5
    threshold = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    client = ScriptedClient(
        [
            _set_plan_turn(["keep counting"]),
            *[_note_turn(index) for index in range(threshold)],
            _answer_turn(),
        ]
    )
    config = LoopConfig(
        steps=(stuckness_step, budget_step, decision_step, stream_step, tool_call_step),
        max_steps=max_steps,
    )

    LoopRunner(client, registry, Bus(), session, 128_000, config).execute()

    nudges = [
        messages[-1].content for messages in client.seen_messages if messages[-1].role is Role.USER
    ]
    assert any("the generation budget is nearly spent" in nudge for nudge in nudges)
    assert _demands(client) == []


def test_ignoring_the_demand_for_the_grace_window_opens_a_crossroads() -> None:
    session, registry = _decision_session()
    client = ScriptedClient(
        [*[_text_turn(f"musing {index}") for index in range(6)], _answer_turn()]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    offered = [{spec.name for spec in specs} for specs in client.seen_tools]
    crossroads = next(
        index for index, names in enumerate(offered) if names == set(CROSSROADS_ACTIONS)
    )
    assert crossroads == DECISION_GRACE + 1
    assert client.seen_messages[crossroads][-1].content.startswith("decision required")


def test_a_shell_call_at_the_crossroads_is_rejected_as_an_invalid_action() -> None:
    session, registry = _decision_session()
    shell_turn: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="s", name="shell", arguments={"command": "true"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    client = ScriptedClient(
        [
            *[_text_turn(f"musing {index}") for index in range(DECISION_GRACE + 1)],
            shell_turn,
            _answer_turn(),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    rejected = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "shell"
    )
    assert rejected.is_error is True
    assert "a decision is required first" in rejected.result
    assert runner.final_answer == "done"


def test_a_plan_set_at_the_crossroads_flows_into_step_orchestration() -> None:
    session, registry = _decision_session()
    client = ScriptedClient(
        [
            *[_text_turn(f"musing {index}") for index in range(DECISION_GRACE + 1)],
            _set_plan_turn(["count the files"]),
            _answer_turn("counted"),
            _answer_turn("all done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is False
    assert recorded.result == "counted"
    assert runner.final_answer == "all done"


def test_heeding_the_demand_within_the_grace_window_avoids_the_crossroads() -> None:
    session, registry = _decision_session()
    client = ScriptedClient(
        [*[_note_turn(index) for index in range(RECENT_UNITS + 3)], _answer_turn()]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    offered = [{spec.name for spec in specs} for specs in client.seen_tools]
    assert all(names != set(CROSSROADS_ACTIONS) for names in offered)
    assert runner.final_answer == "done"


def test_two_ignored_crossroads_end_the_run_with_the_last_narration_unverified() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session, registry = _decision_session()
    client = ScriptedClient([_text_turn(f"musing {index}") for index in range(12)])

    runner = LoopRunner(
        client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.error is None
    assert runner.final_answer is not None
    assert runner.final_answer.startswith(UNVERIFIED_PREFIX)
    assert runner.final_answer.endswith("musing 5")
    settled = next(
        event for event in _drain_until_run_finished(subscriber) if isinstance(event, AnswerSettled)
    )
    assert settled.content == runner.final_answer
    assert settled.accepted is True
    assert settled.verify is None


def test_the_last_words_path_still_offers_answer_alone() -> None:
    session, registry = _decision_session()
    max_steps = 2
    client = ScriptedClient([*_repeat_turns(max_steps), _answer_turn("wrapping up")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, registry, Bus(), session, 128_000, config, depth=MAX_DELEGATE_DEPTH)
    runner.execute()

    assert {spec.name for spec in client.seen_tools[-1]} == {"answer"}
    assert client.seen_messages[-1][-1].content.startswith("this run is ending now")
    assert runner.final_answer == "wrapping up"


def test_set_plan_description_leads_with_the_reason_to_plan() -> None:
    description = next(
        spec.description
        for spec in vocabulary(_decision_session()[1], 0)
        if spec.name == "set_plan"
    )

    assert description.startswith("Break work too big for one context into steps")
    assert description.index("fresh context") < description.index("Replace the current plan")
