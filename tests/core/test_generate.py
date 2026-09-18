import threading
from collections.abc import Iterator

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions, vocabulary
from pico.core.bus import Bus
from pico.core.context import (
    RECENT_UNITS,
    SYSTEM_PROMPT,
    compile_context,
    estimate_tokens,
    message_text,
    prompt_budget,
)
from pico.core.events import (
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
    ToolCallStarted,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.generate import MAX_ACTIONLESS_GENERATIONS, generation_step
from pico.core.loop.prompt import MAX_CHARS_PER_TOKEN, MIN_CHARS_PER_TOKEN, specs_text
from pico.core.loop.runner import LoopConfig, LoopRunner
from pico.core.loop.state import DEFAULT_CHARS_PER_TOKEN, Answered, Failed, Running
from pico.core.stuckness import NUDGE_THRESHOLD
from pico.core.tools import ToolRegistry
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
    RecordingClient,
    ScriptedClient,
    answer_turn,
    drain_until_run_finished,
    echo_registry,
    make_session,
    set_plan_turn,
    stop_turn,
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
            ],
            answer_turn("hello world"),
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="hello "),
        AssistantTextDelta(id="0", text="world"),
        GenerationCompleted(iteration=1),
        AssistantTextFinished(id="0"),
    ]
    assert runner.state == Answered("hello world")
    assert list(session.events())[:2] == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="hello world", thinking=""),
    ]


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
            answer_turn(),
            [
                ThinkingDelta(text="second thought"),
                TextDelta(text="second answer"),
                GenerationComplete(finish_reason="stop"),
            ],
            answer_turn(),
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


def test_generation_step_sends_budget_rendered_messages_to_llm() -> None:
    bus = Bus()
    session = make_session()
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
        LoopConfig(steps=(generation_step,), max_steps=10),
    )
    runner.iterations = 8

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
    runner.decision.crossroads = True

    generation_step(runner)

    assert _published_completion(subscriber) == GenerationCompleted(iteration=2, pressure=True)


def _plan_messages(messages: list[Message]) -> list[Message]:
    return [
        message
        for message in messages
        if message.role is Role.USER and message.content.startswith("Your current plan:")
    ]


def test_no_plan_leaves_message_list_free_of_plan_messages() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")]])

    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_messages[0] == [
        Message(role=Role.SYSTEM, content=SYSTEM_PROMPT),
        Message(role=Role.USER, content="hi"),
    ]


def test_plan_message_tracks_completion_and_appears_exactly_once() -> None:
    session = make_session()
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
    session = make_session()
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
            answer_turn(),
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


def test_generation_step_sends_briefing_and_window_not_the_full_transcript() -> None:
    session = make_session()
    session.append(PlanSet(steps=("keep going",)))
    session.append(PlanStepCompleted(index=0))
    session.append(UserMessageRecorded(content="start"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="noted", is_error=False))
    for index in range(RECENT_UNITS + 4):
        session.append(UserMessageRecorded(content=f"step {index}"))
        session.append(AssistantMessageRecorded(content=f"done {index}", thinking=""))
    client = ScriptedClient([answer_turn()])
    transcript = session.messages()

    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

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


def _sent_overhead(client: RecordingClient, index: int) -> int:
    messages = client.seen_messages[index]
    framing = [
        message
        for message in messages
        if message.role is Role.SYSTEM
        or (
            message.role is Role.USER
            and (
                message.content.startswith("Your current plan:")
                or "you've repeated" in message.content
            )
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


def test_overhead_reflects_the_registered_tool_specs() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = RecordingClient([stop_turn()])
    registry = echo_registry()

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_specs[0] == vocabulary(registry, 0)
    assert _sent_overhead(client, 0) > estimate_tokens(SYSTEM_PROMPT)


def test_overhead_grows_when_a_plan_message_is_present() -> None:
    plain_session = make_session()
    plain_session.append(UserMessageRecorded(content="hi"))
    plain_client = RecordingClient([stop_turn()])
    LoopRunner(
        plain_client, echo_registry(), Bus(), plain_session, 128_000, DEFAULT_LOOP_CONFIG
    ).execute()

    plan_session = make_session("s2")
    plan_session.append(UserMessageRecorded(content="hi"))
    plan_session.append(PlanSet(steps=("one", "two")))
    plan_client = RecordingClient([answer_turn()])
    LoopRunner(
        plan_client, echo_registry(), Bus(), plan_session, 128_000, DEFAULT_LOOP_CONFIG
    ).execute()

    assert _sent_overhead(plan_client, 0) > _sent_overhead(plain_client, 0)


def test_overhead_grows_when_a_nudge_is_present() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(NUDGE_THRESHOLD)
    ]
    turns.append(stop_turn())
    client = RecordingClient(turns)

    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_messages[-1][-1].role is Role.USER
    assert "repeated" in client.seen_messages[-1][-1].content
    assert _sent_overhead(client, len(client.seen_messages) - 1) > _sent_overhead(client, 0)


def test_overhead_shrinks_the_conversation_budget() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="a" * 3_000, is_error=False))
    registry = echo_registry()
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
    client = ScriptedClient([stop_turn()])

    LoopRunner(client, registry, Bus(), session, context_size, DEFAULT_LOOP_CONFIG).execute()

    sent = next(m for m in client.seen_messages[0] if m.role is Role.TOOL)
    unaware = next(m for m in compile_context(session, context_size) if m.role is Role.TOOL)
    assert sent.tool_result is not None
    assert unaware.tool_result is not None
    assert "fact 1" in sent.tool_result.content
    assert unaware.tool_result.content == "a" * 3_000


def test_ratio_moves_toward_the_observed_value() -> None:
    session = make_session()
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
    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert MIN_CHARS_PER_TOKEN <= runner.chars_per_token <= MAX_CHARS_PER_TOKEN
    assert runner.chars_per_token != DEFAULT_CHARS_PER_TOKEN


def test_ratio_clamps_at_the_lower_bound() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                TextDelta(text="done"),
                GenerationComplete(finish_reason="stop", prompt_tokens=1_000_000),
            ]
        ]
    )
    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert runner.chars_per_token == MIN_CHARS_PER_TOKEN


def test_ratio_clamps_at_the_upper_bound() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="x" * 10_000))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=1)]]
    )
    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert runner.chars_per_token == MAX_CHARS_PER_TOKEN


def test_missing_prompt_tokens_leaves_the_ratio_untouched() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([[TextDelta(text="done"), GenerationComplete(finish_reason="stop")]])
    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    assert runner.chars_per_token == DEFAULT_CHARS_PER_TOKEN


def test_second_call_estimates_with_the_updated_ratio() -> None:
    session = make_session()
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
    runner = LoopRunner(client, echo_registry(), Bus(), session, 2_400, DEFAULT_LOOP_CONFIG)
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
    session = make_session()
    session.append(UserMessageRecorded(content="h" * 40_000))
    client = ScriptedClient(
        [
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=9_000)],
            answer_turn(),
        ]
    )

    LoopRunner(client, echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG).execute()

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
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=9_000)]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert not any(isinstance(event, BudgetExceeded) for event in events)
    assert runner.chars_per_token == MIN_CHARS_PER_TOKEN


def test_fitting_prompt_publishes_no_budget_exceeded() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[TextDelta(text="done"), GenerationComplete(finish_reason="stop", prompt_tokens=100)]]
    )

    LoopRunner(client, echo_registry(), bus, session, 8_192, DEFAULT_LOOP_CONFIG).execute()

    events = [next(subscriber) for _ in range(5)]
    assert not any(isinstance(event, BudgetExceeded) for event in events)


def test_narration_without_a_plan_demands_a_decision_instead_of_ending_the_run() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([text_turn("just chatting"), answer_turn("here it is")])

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("here it is")
    demand = client.seen_messages[1][-1]
    assert demand.role is Role.USER
    assert demand.content.startswith("decision required")
    assert "set_plan" in demand.content
    assert "answer" in demand.content
    assert "why" in demand.content


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


def test_long_single_prompt_run_keeps_the_prompt_bounded() -> None:
    session = make_session()
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

    LoopRunner(client, echo_registry(), Bus(), session, 4000, DEFAULT_LOOP_CONFIG).execute()

    final = sum(len(message_text(message)) for message in client.seen_messages[-1])
    full_transcript = sum(len(message_text(message)) for message in session.messages())
    assert final < full_transcript / 2
    assert final <= prompt_budget(4000) * 6
    task = client.seen_messages[-1][2]
    assert task.role is Role.USER
    assert task.content == "review everything"
