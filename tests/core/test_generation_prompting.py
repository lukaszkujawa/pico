from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions, vocabulary
from pico.core.bus import Bus
from pico.core.events import BudgetExceeded, BusEvent, GenerationCompleted, RunFinished
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.prompt import (
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    SYSTEM_PROMPT,
    compile_context,
    message_text,
    specs_text,
)
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import DEFAULT_CHARS_PER_TOKEN
from pico.core.loop.stuckness import NUDGE_THRESHOLD
from pico.core.tools import ToolRegistry
from pico.llm.budget import estimate_tokens, prompt_budget
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    StreamEvent,
    TextDelta,
    ToolCall,
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
    RecordingClient,
    ScriptedClient,
    answer_turn,
    context_size_for_budget,
    echo_registry,
    make_session,
    sent_overhead,
    stop_turn,
)


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


def test_runner_keeps_a_demoted_result_byte_stable_across_generations() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="x" * 12_000, is_error=False)
    )
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="echo", arguments={"text": "hi"})),
                GenerationComplete(
                    finish_reason="tool_calls", prompt_tokens=150, completion_tokens=5
                ),
            ],
            stop_turn(),
        ]
    )
    runner = LoopRunner(client, echo_registry(), Bus(), session, 4_000, DEFAULT_LOOP_CONFIG)

    runner.execute()

    first = next(m for m in client.seen_messages[0] if m.role is Role.TOOL)
    second = next(m for m in client.seen_messages[1] if m.role is Role.TOOL)
    assert first.tool_result is not None
    assert "read_fact(1)" in first.tool_result.content
    assert second == first


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
    for index in range(12):
        session.append(UserMessageRecorded(content=f"step {index} " + "x" * 1_000))
        session.append(
            AssistantMessageRecorded(content=f"done {index} " + "x" * 1_000, thinking="")
        )
    client = ScriptedClient([answer_turn()])
    transcript = session.messages()

    LoopRunner(client, echo_registry(), Bus(), session, 2_048, DEFAULT_LOOP_CONFIG).execute()

    sent = client.seen_messages[0]
    assert sent[0].role is Role.SYSTEM
    briefing = sent[-2]
    assert briefing.content.startswith("Your current plan:")
    assert "Facts gathered so far:" in briefing.content
    window = sent[1:-2]
    assert 0 < len(window) < len(transcript)
    assert window == [transcript[0], *transcript[-(len(window) - 1) :]]
    assert sent[-1].content.startswith("decision required")
    assert sum("Your current plan:" in message.content for message in sent) == 1


def test_overhead_reflects_the_registered_tool_specs() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = RecordingClient([stop_turn()])
    registry = echo_registry()

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert client.seen_specs[0] == vocabulary(registry, 0)
    assert sent_overhead(client, 0) > estimate_tokens(SYSTEM_PROMPT)


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

    assert sent_overhead(plan_client, 0) > sent_overhead(plain_client, 0)


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
    assert sent_overhead(client, len(client.seen_messages) - 1) > sent_overhead(client, 0)


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
        message_text(compile_context(session, 128_000)[-1]), DEFAULT_CHARS_PER_TOKEN
    )
    context_size = context_size_for_budget(conversation + briefing + overhead - 1)
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
    task = client.seen_messages[-1][1]
    assert task.role is Role.USER
    assert task.content == "review everything"
