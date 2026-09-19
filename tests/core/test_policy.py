from pico.core.actions import MAX_DELEGATE_DEPTH
from pico.core.bus import Bus
from pico.core.context import transcript_fullness
from pico.core.events import (
    AnswerSettled,
    ErrorOccurred,
    RunFinished,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG, DEFAULT_LOOP_STEPS
from pico.core.loop.decision import (
    CROSSROADS_ACTIONS,
    DECISION_GRACE,
    NARRATION_PRESSURE,
    Crossroads,
    Quiet,
)
from pico.core.loop.dispatch import tool_call_step
from pico.core.loop.generate import generation_step
from pico.core.loop.policy import (
    BUDGET_PRESSURE,
    BUDGET_WIND_DOWN_FRACTION,
    CONTEXT_PRESSURE_CAUSE,
    LAST_WORDS_ACTIONS,
    Verdict,
    apply_verdict,
    budget_remaining,
    budget_rule,
    felt_pressure,
    observe,
    policy_step,
    restriction,
    undecided,
)
from pico.core.loop.runner import LoopConfig, LoopRunner
from pico.core.loop.signals import Nudge
from pico.core.loop.state import Answered, Failed, LastWords, Running, WindingDown
from pico.core.loop.subruns import (
    child_budget,
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
    PlanSet,
    PlanStepCompleted,
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


def test_budget_remaining_counts_down_only_for_a_running_root_with_a_budget() -> None:
    assert budget_remaining(1_000_000, None, 0, True) is None
    assert budget_remaining(8, 10, 1, True) is None
    assert budget_remaining(8, 10, 0, False) is None
    assert budget_remaining(7, 10, 0, True) is None
    assert budget_remaining(8, 10, 0, True) == 2


def test_undecided_tracks_the_plan_in_the_session() -> None:
    session = make_session()
    assert undecided(session) is True
    session.append(PlanSet(steps=("count the files",)))
    assert undecided(session) is False
    session.append(PlanStepCompleted(index=0))
    assert undecided(session) is True


def test_pressure_prefers_narration_then_context() -> None:
    assert felt_pressure(True, 0.9) == NARRATION_PRESSURE
    assert felt_pressure(False, 0.9) == CONTEXT_PRESSURE_CAUSE
    assert felt_pressure(False, 0.1) is None


def test_the_view_folds_the_iteration_signals_into_one_snapshot() -> None:
    max_steps = 10
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        session,
        128_000,
        LoopConfig(steps=(), max_steps=max_steps),
    )
    runner.iterations = int(max_steps * BUDGET_WIND_DOWN_FRACTION)

    assert policy_step(runner) == "continue"

    view = runner.view
    assert view.iterations == runner.iterations
    assert view.remaining == 2
    assert view.fullness == transcript_fullness(session.messages(), 128_000, runner.chars_per_token)
    assert view.undecided is True
    assert view.pressure == BUDGET_PRESSURE.format(remaining=2)
    assert view.pressed is True


def test_the_view_reports_no_pressure_for_a_quiet_iteration() -> None:
    runner = LoopRunner(
        FailingClient(), echo_registry(), Bus(), make_session(), 128_000, LoopConfig(steps=())
    )
    runner.state = WindingDown("stuck")

    policy_step(runner)

    assert runner.view.remaining is None
    assert runner.view.undecided is False
    assert runner.view.pressure is None
    assert runner.view.pressed is False


def test_observe_builds_the_same_view_twice() -> None:
    session, _ = decision_session()
    runner = LoopRunner(
        FailingClient(), echo_registry(), Bus(), session, 128_000, LoopConfig(steps=())
    )
    runner.generation.narration_pressure = True
    runner.generation.last_narration = "musing"

    first = observe(runner)
    second = observe(runner)

    assert first == second
    assert first.pressure == NARRATION_PRESSURE
    assert first.narration == "musing"


def test_restriction_prefers_last_words_over_a_crossroads() -> None:
    restrict = restriction(LastWords("the budget is spent"), Crossroads("why", 1), "a nudge")
    assert restrict is not None
    assert restrict.allowed == LAST_WORDS_ACTIONS
    assert "the budget is spent" in restrict.text


def test_restriction_at_a_crossroads_keeps_the_pending_nudge_text() -> None:
    assert restriction(Running(), Quiet(), "a nudge") is None
    restrict = restriction(Running(), Crossroads("why", 1), None)
    assert restrict is not None
    assert restrict.allowed == CROSSROADS_ACTIONS
    assert "why" in restrict.text
    nudged = restriction(Running(), Crossroads("why", 1), "a nudge")
    assert nudged is not None
    assert nudged.text == "a nudge"


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

    assert isinstance(runner.state, Failed)
    assert "stuck" in runner.state.reason
    events = drain_until_run_finished(subscriber)
    assert ErrorOccurred(message=runner.state.reason) in events
    assert events[-1] == RunFinished(error=runner.state.reason)


def test_budget_rule_is_silent_when_max_steps_is_none() -> None:
    runner = LoopRunner(
        FailingClient(), echo_registry(), Bus(), make_session(), 128_000, LoopConfig(steps=())
    )
    runner.iterations = 1_000_000
    runner.view = observe(runner)
    assert budget_rule(runner) == Verdict()


def test_budget_rule_is_silent_for_delegates_regardless_of_iterations() -> None:
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        make_session(),
        128_000,
        LoopConfig(steps=(), max_steps=child_budget(2)),
        depth=1,
    )
    runner.iterations = child_budget(2)
    runner.view = observe(runner)
    assert budget_rule(runner) == Verdict()


def test_budget_rule_below_wind_down_threshold_is_silent() -> None:
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
    runner.view = observe(runner)
    assert budget_rule(runner) == Verdict()


def test_budget_rule_at_wind_down_threshold_speaks_nudge_and_pressure_together() -> None:
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
    runner.view = observe(runner)
    verdict = budget_rule(runner)
    remaining = max_steps - runner.iterations
    [nudge] = verdict.nudges
    assert str(remaining) in nudge
    assert "answer" in nudge
    assert verdict.pressure == BUDGET_PRESSURE.format(remaining=remaining)

    assert apply_verdict(runner, verdict) == "continue"
    assert runner.view.pressure == verdict.pressure
    assert [pending.text for pending in runner.pending_nudges] == [nudge]


def test_budget_pressure_yields_to_a_stronger_pressure_already_in_the_view() -> None:
    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        Bus(),
        make_session(),
        128_000,
        LoopConfig(steps=(), max_steps=10),
    )
    runner.generation.narration_pressure = True
    runner.iterations = 8
    runner.view = observe(runner)

    apply_verdict(runner, budget_rule(runner))

    assert runner.view.pressure == NARRATION_PRESSURE


def test_policy_steps_accumulate_their_nudges() -> None:
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

    assert policy_step(runner) == "continue"

    first, second = runner.pending_nudges
    assert "the generation budget is nearly spent" in first.text
    assert second.text.startswith("decision required")


def test_taking_the_nudges_clears_the_list() -> None:
    runner = LoopRunner(
        FailingClient(), echo_registry(), Bus(), make_session(), 128_000, LoopConfig(steps=())
    )
    runner.emit(Nudge("first"))
    runner.emit(Nudge("second"))

    assert runner.take_nudges() == [Nudge("first"), Nudge("second")]
    assert runner.take_nudges() == []


def test_budget_rule_leaves_the_kill_to_the_last_words_generation() -> None:
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

    runner.view = observe(runner)
    assert budget_rule(runner).transition is None
    assert runner.state == Running()


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
        steps=(policy_step, generation_step, tool_call_step),
        max_steps=max_steps,
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, config)
    runner.execute()

    last_messages = client.seen_messages[-1]
    nudges = [m for m in last_messages if m.role is Role.USER and "generation budget" in m.content]
    assert len(nudges) == 1
    assert runner.state == Answered("done")


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
        steps=(policy_step, generation_step, tool_call_step),
        max_steps=max_steps,
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, config)
    runner.execute()

    expected = f"run stopped: the generation budget of {max_steps} is spent"
    assert runner.state == Failed(expected)
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

    assert followup_runner.state == Answered("picked up")
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

    assert runner.state == Answered(
        "what I found so far", f"the generation budget of {max_steps} is spent"
    )
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

    assert isinstance(runner.state, Answered)
    assert runner.state.content == "partial findings"
    assert runner.state.cause is not None
    assert "stuck" in runner.state.cause


def test_a_non_answer_call_in_the_final_generation_keeps_the_original_failure() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    max_steps = 2
    turns = [*repeat_turns(max_steps + 1)]
    client = ScriptedClient(turns)
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, config)
    runner.execute()

    assert runner.state == Failed(f"run stopped: the generation budget of {max_steps} is spent")
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

    assert runner.state == Failed(f"run stopped: the generation budget of {max_steps} is spent")


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
        [
            *[note_turn(index, f"finding {index} " + "x" * 4_000) for index in range(4)],
            answer_turn(),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 2_048, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
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
            *[note_turn(index, f"finding {index} " + "x" * 4_000) for index in range(4)],
            answer_turn(),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 2_048, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert decision_demands(client) == []


def test_the_root_at_wind_down_with_no_plan_gets_the_demand_and_the_wind_down_nudge() -> None:
    session, registry = decision_session()
    max_steps = 5
    threshold = int(max_steps * BUDGET_WIND_DOWN_FRACTION)
    client = ScriptedClient([*[note_turn(index) for index in range(threshold)], answer_turn()])
    config = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps)

    LoopRunner(client, registry, Bus(), session, 128_000, config).execute()

    demands = decision_demands(client)
    assert len(demands) == 1
    assert "generations remain of your budget" in demands[0]
    assert "the generation budget is nearly spent" in demands[0]


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
        steps=(policy_step, generation_step, tool_call_step),
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
    assert runner.state == Answered("done")


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
    assert runner.state == Answered("all done")


def test_heeding_the_demand_within_the_grace_window_avoids_the_crossroads() -> None:
    session, registry = decision_session()
    client = ScriptedClient(
        [
            *[note_turn(index, f"finding {index} " + "x" * 4_000) for index in range(4)],
            answer_turn(),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 2_048, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    offered = [{spec.name for spec in specs} for specs in client.seen_tools]
    assert all(names != set(CROSSROADS_ACTIONS) for names in offered)
    assert runner.state == Answered("done")


def test_two_ignored_crossroads_end_the_run_with_the_last_narration_unverified() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session, registry = decision_session()
    client = ScriptedClient([text_turn(f"musing {index}") for index in range(12)])

    runner = LoopRunner(
        client, registry, bus, session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.state == Answered("musing 5")
    settled = next(
        event for event in drain_until_run_finished(subscriber) if isinstance(event, AnswerSettled)
    )
    assert settled.content == "musing 5"
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
    assert runner.state == Answered("wrapping up", f"the generation budget of {max_steps} is spent")
