import threading
from collections.abc import Iterator

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions
from pico.core.bus import Bus
from pico.core.events import (
    AnswerSettled,
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BusEvent,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.decision import NARRATION_PRESSURE, Crossroads, IterationView
from pico.core.loop.generate import (
    MAX_ACTIONLESS_GENERATIONS,
    NO_ACTION_NUDGE,
    Emit,
    Fail,
    Generation,
    Press,
    Verdict,
    generation_step,
    record,
)
from pico.core.loop.policy import policy_step
from pico.core.loop.runner import LoopConfig, LoopRunner
from pico.core.loop.state import (
    Answered,
    Failed,
    GenerationState,
    LastWords,
    Running,
    WindingDown,
)
from pico.core.tools import ToolRegistry
from pico.llm.types import (
    GenerationComplete,
    Role,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
)
from pico.session import (
    AssistantMessageRecorded,
    PlanSet,
    PlanStepCompleted,
    ToolCallRecorded,
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    CancellingClient,
    FailingClient,
    ScriptedClient,
    answer_turn,
    drain_until_run_finished,
    echo_registry,
    echo_turn,
    make_session,
    set_plan_turn,
    text_turn,
)


def test_plain_text_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                TextDelta(text="hello "),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ]
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert drain_until_run_finished(subscriber) == [
        RunStarted(),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="hello "),
        AssistantTextDelta(id="0", text="world"),
        GenerationCompleted(iteration=1),
        AssistantTextFinished(id="0"),
        ToolCallStarted(id="1", name="answer", arguments={}),
        AnswerSettled(id="1", content="hello world", accepted=True, complete=True),
        RunFinished(),
    ]
    assert runner.state == Answered("hello world")
    assert list(session.events()) == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="hello world", thinking=""),
    ]
    assert runner.iterations == 1


def test_thinking_then_text_published_in_order_with_shared_ids_across_two_turns() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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
        GenerationCompleted(iteration=1),
        AssistantThinkingFinished(id="0"),
        AssistantTextFinished(id="1"),
    ]

    runner.state = Running()
    runner.execute()

    second_events = [next(subscriber) for _ in range(11)]
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


def test_single_tool_call_round_trip() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
            answer_turn(),
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(8)]
    assert events == [
        RunStarted(),
        GenerationCompleted(iteration=1),
        ToolCallStarted(id="0", name="echo", arguments={"text": "hi"}),
        ToolCallFinished(id="0", tool_call=call, result="hi", is_error=False, fact_id=1),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="done"),
        GenerationCompleted(iteration=2),
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
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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
    session = make_session()
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
            answer_turn(),
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        ToolCallArgumentsDelta(id="0", name="echo", text='{"text":'),
        GenerationCompleted(iteration=1),
        ToolCallStarted(id="0", name="echo", arguments={"text": "hi"}),
        ToolCallFinished(id="0", tool_call=call, result="hi", is_error=False, fact_id=1),
        GenerationCompleted(iteration=2),
    ]


def test_llm_error_surfaced_as_error_occurred() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = FailingClient()

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    cancel.set()
    client = CancellingClient(
        events=[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")],
        cancel=cancel,
        cancel_after=-1,
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel)
    runner.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]
    assert list(session.events()) == [UserMessageRecorded(content="hi")]


def test_cancel_mid_stream_stops_consuming_and_closes_open_panes() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel)
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
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    client = CancellingClient(
        events=[TextDelta(text="hi"), GenerationComplete(finish_reason="stop")],
        cancel=cancel,
        cancel_after=0,
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel)
    runner.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]
    assert not any(isinstance(event, RunFinished) for event in events)


def test_each_llm_call_publishes_its_own_token_counts() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(9)]
    completions = [event for event in events if isinstance(event, GenerationCompleted)]
    assert completions == [
        GenerationCompleted(prompt_tokens=120, completion_tokens=17, iteration=1),
        GenerationCompleted(prompt_tokens=160, completion_tokens=4, iteration=2),
    ]


def _published_completion(subscriber: Iterator[BusEvent]) -> GenerationCompleted:
    while True:
        event = next(subscriber)
        if isinstance(event, GenerationCompleted):
            return event


def test_generation_publishes_pressure_once_the_budget_winds_down() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([text_turn("nearly done")])
    runner = LoopRunner(
        client,
        echo_registry(),
        bus,
        session,
        128_000,
        LoopConfig(steps=(generation_step,), budgets=(10,)),
    )
    runner.iterations = 8
    policy_step(runner)

    generation_step(runner)

    assert _published_completion(subscriber) == GenerationCompleted(iteration=8, pressure=True)


def test_generation_publishes_pressure_at_a_decision_crossroads() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([text_turn("mulling it over")])
    runner = LoopRunner(
        client, echo_registry(), bus, session, 128_000, LoopConfig(steps=(generation_step,))
    )
    runner.iterations = 2
    runner.decision = Crossroads(NARRATION_PRESSURE, 1)

    generation_step(runner)

    assert _published_completion(subscriber) == GenerationCompleted(iteration=2, pressure=True)


def test_generation_turns_a_wind_down_into_last_words_in_the_same_iteration() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([text_turn("all I know")])
    runner = LoopRunner(
        client, echo_registry(), Bus(), session, 128_000, LoopConfig(steps=(generation_step,))
    )
    runner.state = WindingDown("stuck")

    assert generation_step(runner) == "done"

    assert runner.state == LastWords("stuck")
    assert client.seen_messages[-1][-1].content.startswith("this run is ending now")
    assert [spec.name for spec in client.seen_tools[-1]] == ["answer"]


def test_generation_owns_the_narration_pressure_flag() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([text_turn("musing"), echo_turn()])
    runner = LoopRunner(
        client, echo_registry(), Bus(), session, 128_000, LoopConfig(steps=(generation_step,))
    )
    runner.view = IterationView(undecided=True)

    generation_step(runner)
    assert runner.generation.narration_pressure is True

    generation_step(runner)
    assert runner.generation.narration_pressure is False


def _silence(text: str = "musing") -> Generation:
    return Generation(text=text, thinking="", tool_calls=[])


def _counted(actionless: int) -> GenerationState:
    return GenerationState(actionless_generations=actionless)


def test_record_of_a_tool_call_resets_the_actionless_count() -> None:
    generation = Generation(
        text="", thinking="", tool_calls=[ToolCall(id="1", name="echo", arguments={})]
    )
    recorded = record(generation, Running(), IterationView(), _counted(2), opening=True)
    assert recorded == Verdict("continue", actionless=0)


def test_record_of_silence_while_dying_ends_the_run() -> None:
    recorded = record(_silence(), LastWords("spent"), IterationView(), _counted(1), opening=True)
    assert recorded == Verdict("done", actionless=1)


def test_record_of_narration_without_a_plan_presses_for_a_decision() -> None:
    recorded = record(_silence(), Running(), IterationView(undecided=True), _counted(2), False)
    assert recorded == Verdict("continue", actionless=0, command=Press())


def test_record_counts_actionless_generations_up_to_the_failure() -> None:
    recorded = record(_silence(), Running(), IterationView(), _counted(0), False)
    assert recorded == Verdict("continue", actionless=1, command=Emit(NO_ACTION_NUDGE))
    final = record(
        _silence(), Running(), IterationView(), _counted(MAX_ACTIONLESS_GENERATIONS - 1), False
    )
    assert final.outcome == "done"
    assert final.actionless == MAX_ACTIONLESS_GENERATIONS
    assert isinstance(final.command, Fail)
    assert "without a tool call" in final.command.reason


def test_narration_without_a_plan_demands_a_decision_instead_of_ending_the_run() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [echo_turn("hi"), text_turn("just chatting"), answer_turn("here it is")]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("here it is")
    demand = client.seen_messages[2][-1]
    assert demand.role is Role.USER
    assert demand.content.startswith("decision required")
    assert "set_plan" in demand.content
    assert "answer" in demand.content
    assert "why" not in demand.content


def test_first_text_only_generation_is_adopted_as_the_answer() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="Hi Pico"))
    client = ScriptedClient([text_turn("Hi! What can I do for you?")])

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("Hi! What can I do for you?")
    assert runner.iterations == 1
    settled = [
        event for event in drain_until_run_finished(subscriber) if isinstance(event, AnswerSettled)
    ]
    assert settled == [
        AnswerSettled(id="1", content="Hi! What can I do for you?", accepted=True, complete=True)
    ]


def test_first_generation_with_a_tool_call_is_not_adopted() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([echo_turn(), answer_turn("done")])

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("done")
    assert runner.iterations == 2


def test_first_text_only_generation_with_a_plan_already_set_is_not_adopted() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(PlanSet(steps=("review the code",)))
    session.append(PlanStepCompleted(index=0))
    client = ScriptedClient([text_turn("let me think"), answer_turn("reviewed")])

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("reviewed")
    assert client.seen_messages[1][-1].content.startswith("decision required")


def test_a_childs_first_text_only_generation_is_pressed_not_adopted() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="count the files"))
    client = ScriptedClient([text_turn("just chatting"), answer_turn("42")])

    runner = LoopRunner(
        client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=1
    )
    runner.execute()

    assert runner.state == Answered("42")
    demand = client.seen_messages[1][-1]
    assert demand.content.startswith("decision required")


def test_a_thinking_only_first_generation_is_not_adopted() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [ThinkingDelta(text="pondering"), GenerationComplete(finish_reason="stop")],
            answer_turn("settled"),
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("settled")
    demand = client.seen_messages[1][-1]
    assert demand.content.startswith("decision required")


def test_narration_with_unfinished_plan_is_nudged_and_run_continues() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    registry = ToolRegistry()
    register_actions(registry, session)
    client = ScriptedClient(
        [
            set_plan_turn(["review the code"]),
            text_turn("now let me look at the next file"),
            answer_turn("all reviewed"),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.state == Answered("all reviewed")
    nudge = client.seen_messages[2][-1]
    assert nudge.role is Role.USER
    assert "took no action" in nudge.content


def test_repeated_narration_with_unfinished_plan_stops_the_run_with_an_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    registry = ToolRegistry()
    register_actions(registry, session)
    turns = [
        set_plan_turn(["review the code"]),
        *[text_turn(f"musing {i}") for i in range(MAX_ACTIONLESS_GENERATIONS)],
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(
        client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert isinstance(runner.state, Failed)
    assert "without a tool call" in runner.state.reason
    assert drain_until_run_finished(subscriber)[-1] == RunFinished(error=runner.state.reason)
