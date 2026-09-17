from pico.core.actions import MAX_DELEGATE_DEPTH
from pico.core.bus import Bus
from pico.core.context import (
    RECENT_UNITS,
)
from pico.core.events import (
    AnswerSettled,
    ErrorOccurred,
    RunFinished,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG, DEFAULT_LOOP_STEPS
from pico.core.loop.dispatch import tool_call_step
from pico.core.loop.generate import generation_step
from pico.core.loop.policy import (
    BUDGET_WIND_DOWN_FRACTION,
    CONTEXT_PRESSURE_CAUSE,
    CROSSROADS_ACTIONS,
    DECISION_GRACE,
    UNVERIFIED_PREFIX,
    budget_step,
    decision_step,
    stuckness_step,
)
from pico.core.loop.runner import LoopConfig, LoopRunner
from pico.core.loop.signals import Nudge
from pico.core.loop.subruns import (
    MAX_DELEGATE_STEPS,
)
from pico.core.stuckness import NUDGE_THRESHOLD, STUCK_THRESHOLD
from pico.llm.types import (
    GenerationComplete,
    Role,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
)
from pico.session import (
    ToolCallRecorded,
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    FailingClient,
    ScriptedClient,
    answer_turn,
    decision_demands,
    decision_session,
    drain_until_run_finished,
    echo_registry,
    make_session,
    note_turn,
    repeat_turns,
    set_plan_turn,
    text_turn,
)


def test_nudge_below_stuck_threshold_is_appended_as_user_message_not_persisted() -> None:
    bus = Bus()
    session = make_session()
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

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
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
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient([[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")]])

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    sent_messages = client.seen_messages[0]
    assert all(
        message.role is not Role.USER or message.content == "hi" for message in sent_messages
    )


def test_stuck_run_carries_its_reason_on_run_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(STUCK_THRESHOLD)
    ]
    turns.append(text_turn("I have nothing to add"))
    runner = LoopRunner(
        ScriptedClient(turns), echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG
    )
    runner.execute()

    assert runner.error is not None
    assert "stuck" in runner.error
    events = drain_until_run_finished(subscriber)
    assert ErrorOccurred(message=runner.error) in events
    assert events[-1] == RunFinished(error=runner.error)


def test_budget_step_is_noop_when_max_steps_is_none() -> None:
    runner = LoopRunner(
        FailingClient(), echo_registry(), Bus(), make_session(), 128_000, LoopConfig(steps=())
    )
    runner.iterations = 1_000_000
    assert budget_step(runner) == "continue"
    assert runner.pending_signal is None
    assert runner.error is None


def test_budget_step_is_noop_for_delegates_regardless_of_iterations() -> None:
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        make_session(),
        128_000,
        LoopConfig(steps=(), max_steps=MAX_DELEGATE_STEPS),
        depth=1,
    )
    runner.iterations = MAX_DELEGATE_STEPS
    assert budget_step(runner) == "continue"
    assert runner.pending_signal is None
    assert runner.error is None


def test_budget_step_below_wind_down_threshold_sets_no_nudge() -> None:
    max_steps = 10
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        make_session(),
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = int(max_steps * BUDGET_WIND_DOWN_FRACTION) - 1
    assert budget_step(runner) == "continue"
    assert runner.pending_signal is None


def test_budget_step_at_wind_down_threshold_sets_nudge_with_remaining_count() -> None:
    max_steps = 10
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        make_session(),
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    outcome = budget_step(runner)
    remaining = max_steps - runner.iterations
    assert outcome == "continue"
    assert isinstance(runner.pending_signal, Nudge)
    assert str(remaining) in runner.pending_signal.text
    assert "answer" in runner.pending_signal.text


def test_the_last_policy_step_to_emit_wins_the_signal_slot() -> None:
    max_steps = 10
    session = make_session()
    session.append(UserMessageRecorded(content="task"))
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        session,
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = int(max_steps * BUDGET_WIND_DOWN_FRACTION)

    assert budget_step(runner) == "continue"
    assert isinstance(runner.pending_signal, Nudge)
    assert "the generation budget is nearly spent" in runner.pending_signal.text

    assert decision_step(runner) == "continue"
    assert isinstance(runner.pending_signal, Nudge)
    assert runner.pending_signal.text.startswith("decision required")


def test_taking_the_signal_clears_the_slot() -> None:
    runner = LoopRunner(
        FailingClient(), echo_registry(), Bus(), make_session(), 128_000, LoopConfig(steps=())
    )
    runner.emit(Nudge("first"))
    runner.emit(Nudge("second"))

    assert runner.take_signal() == Nudge("second")
    assert runner.take_signal() is None


def test_budget_step_leaves_the_kill_to_the_last_words_generation() -> None:
    max_steps = 10
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        make_session(),
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = max_steps

    assert budget_step(runner) == "continue"
    assert runner.error is None


def test_run_reaching_soft_threshold_gets_wind_down_nudge_then_answers_normally() -> None:
    bus = Bus()
    session = make_session()
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
        steps=(stuckness_step, budget_step, generation_step, tool_call_step), max_steps=max_steps
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, config)
    runner.execute()

    last_messages = client.seen_messages[-1]
    nudges = [m for m in last_messages if m.role is Role.USER and "generation budget" in m.content]
    assert len(nudges) == 1
    assert runner.final_answer == "done"
    assert runner.error is None


def test_run_exhausting_budget_fails_explicitly_and_stays_resumable() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 3
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"i": i})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(max_steps)
    ]
    turns.append(text_turn("I have nothing to say"))
    client = ScriptedClient(turns)
    config = LoopConfig(
        steps=(stuckness_step, budget_step, generation_step, tool_call_step), max_steps=max_steps
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, config)
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
            answer_turn("picked up"),
        ]
    )
    followup_runner = LoopRunner(followup_client, echo_registry(), Bus(), session, 128_000, config)
    followup_runner.execute()

    assert followup_runner.error is None
    assert followup_runner.final_answer == "picked up"
    echoes = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "echo"
    ]
    assert len(echoes) == max_steps


def test_budget_exhaustion_forces_a_final_answer_that_becomes_the_result() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 3
    turns = [*repeat_turns(max_steps), answer_turn("what I found so far")]
    client = ScriptedClient(turns)
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.final_answer == "what I found so far"
    assert runner.error is None
    nudge = client.seen_messages[-1][-1]
    assert nudge.role is Role.USER
    assert "final generation" in nudge.content
    assert "budget" in nudge.content


def test_the_final_generation_offers_only_the_answer_tool() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    client = ScriptedClient([*repeat_turns(max_steps), answer_turn("all I have")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, echo_registry(), Bus(), session, 128_000, config).execute()

    assert [spec.name for spec in client.seen_tools[-1]] == ["answer"]
    assert len(client.seen_tools[0]) > 1


def test_a_stuck_node_answers_before_the_stuck_failure() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns = [*repeat_turns(STUCK_THRESHOLD), answer_turn("partial findings")]

    runner = LoopRunner(
        ScriptedClient(turns), echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG
    )
    runner.execute()

    assert runner.final_answer == "partial findings"
    assert runner.error is None


def test_a_non_answer_call_in_the_final_generation_keeps_the_original_failure() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    turns = [*repeat_turns(max_steps + 1)]
    client = ScriptedClient(turns)
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.final_answer is None
    assert runner.error == f"run stopped: the generation budget of {max_steps} is spent"
    rejected = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert rejected.is_error is True
    assert "answer is the only tool left" in rejected.result


def test_silence_in_the_final_generation_fails_with_the_original_error() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    client = ScriptedClient([*repeat_turns(max_steps), text_turn("no comment")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.final_answer is None
    assert runner.error == f"run stopped: the generation budget of {max_steps} is spent"


def test_the_wind_down_nudge_still_fires_before_the_final_generation() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 5
    client = ScriptedClient([*repeat_turns(max_steps), answer_turn("wrapping up")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, echo_registry(), Bus(), session, 128_000, config).execute()

    nudges = [
        message.content
        for messages in client.seen_messages
        for message in messages[-1:]
        if message.role is Role.USER and "generation budget" in message.content
    ]
    assert any("generations remain" in nudge for nudge in nudges)
    assert "final generation" in client.seen_messages[-1][-1].content


def test_transcript_past_the_structural_bound_demands_a_decision_once() -> None:
    session, registry = decision_session()
    client = ScriptedClient(
        [*[note_turn(index) for index in range(RECENT_UNITS + 3)], answer_turn()]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    demands = decision_demands(client)
    assert len(demands) == 1
    assert CONTEXT_PRESSURE_CAUSE in demands[0]
    assert "set_plan" in demands[0] and "answer" in demands[0] and "why" in demands[0]


def test_a_node_with_an_active_plan_never_sees_the_demand() -> None:
    session, registry = decision_session()
    client = ScriptedClient(
        [
            set_plan_turn(["keep counting"]),
            *[note_turn(index) for index in range(RECENT_UNITS + 3)],
            answer_turn(),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert decision_demands(client) == []


def test_the_root_at_wind_down_with_no_plan_gets_the_demand_not_the_wind_down_nudge() -> None:
    session, registry = decision_session()
    max_steps = 5
    threshold = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    client = ScriptedClient([*[note_turn(index) for index in range(threshold)], answer_turn()])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, registry, Bus(), session, 128_000, config).execute()

    demands = decision_demands(client)
    assert len(demands) == 1
    assert "generations remain of your budget" in demands[0]


def test_the_root_at_wind_down_with_unfinished_steps_keeps_the_wind_down_wording() -> None:
    session, registry = decision_session()
    max_steps = 5
    threshold = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    client = ScriptedClient(
        [
            set_plan_turn(["keep counting"]),
            *[note_turn(index) for index in range(threshold)],
            answer_turn(),
        ]
    )
    config = LoopConfig(
        steps=(stuckness_step, budget_step, decision_step, generation_step, tool_call_step),
        max_steps=max_steps,
    )

    LoopRunner(client, registry, Bus(), session, 128_000, config).execute()

    nudges = [
        messages[-1].content for messages in client.seen_messages if messages[-1].role is Role.USER
    ]
    assert any("the generation budget is nearly spent" in nudge for nudge in nudges)
    assert decision_demands(client) == []


def test_ignoring_the_demand_for_the_grace_window_opens_a_crossroads() -> None:
    session, registry = decision_session()
    client = ScriptedClient([*[text_turn(f"musing {index}") for index in range(6)], answer_turn()])

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
    session, registry = decision_session()
    shell_turn: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="s", name="shell", arguments={"command": "true"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    client = ScriptedClient(
        [
            *[text_turn(f"musing {index}") for index in range(DECISION_GRACE + 1)],
            shell_turn,
            answer_turn(),
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
    session, registry = decision_session()
    client = ScriptedClient(
        [
            *[text_turn(f"musing {index}") for index in range(DECISION_GRACE + 1)],
            set_plan_turn(["count the files"]),
            answer_turn("counted"),
            answer_turn("all done"),
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
    session, registry = decision_session()
    client = ScriptedClient(
        [*[note_turn(index) for index in range(RECENT_UNITS + 3)], answer_turn()]
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
    session, registry = decision_session()
    client = ScriptedClient([text_turn(f"musing {index}") for index in range(12)])

    runner = LoopRunner(
        client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.error is None
    assert runner.final_answer is not None
    assert runner.final_answer.startswith(UNVERIFIED_PREFIX)
    assert runner.final_answer.endswith("musing 5")
    settled = next(
        event for event in drain_until_run_finished(subscriber) if isinstance(event, AnswerSettled)
    )
    assert settled.content == runner.final_answer
    assert settled.accepted is True
    assert settled.verify is None


def test_the_last_words_path_still_offers_answer_alone() -> None:
    session, registry = decision_session()
    max_steps = 2
    client = ScriptedClient([*repeat_turns(max_steps), answer_turn("wrapping up")])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, registry, Bus(), session, 128_000, config, depth=MAX_DELEGATE_DEPTH)
    runner.execute()

    assert {spec.name for spec in client.seen_tools[-1]} == {"answer"}
    assert client.seen_messages[-1][-1].content.startswith("this run is ending now")
    assert runner.final_answer == "wrapping up"
