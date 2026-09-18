from pico.core.stuckness import (
    NUDGE_THRESHOLD,
    PLAN_STALL_GENERATIONS,
    STUCK_THRESHOLD,
    WINDOW_GENERATIONS,
    Stuckness,
    assess,
    generations_since_plan_event,
    repeated_action_streak,
    tool_failure_streak,
    windowed_repeat,
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


def _session() -> Session:
    conn = connect(":memory:")
    return Session(conn, "s1")


def _call(
    name: str = "shell", arguments: dict[str, object] | None = None, is_error: bool = False
) -> ToolCallRecorded:
    return ToolCallRecorded(
        name=name, arguments=arguments or {}, result="result", is_error=is_error
    )


def test_streaks_empty_session_are_zero() -> None:
    session = _session()
    assert repeated_action_streak(session) == 0
    assert tool_failure_streak(session) == 0


def test_repeated_action_streak_single_call() -> None:
    session = _session()
    session.append(_call())
    assert repeated_action_streak(session) == 1
    assert tool_failure_streak(session) == 0


def test_repeated_action_streak_resets_on_different_call() -> None:
    session = _session()
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(_call(name="shell", arguments={"command": "b"}))
    assert repeated_action_streak(session) == 1


def test_repeated_action_streak_counts_consecutive_identical_calls() -> None:
    session = _session()
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(_call(name="shell", arguments={"command": "a"}))
    assert repeated_action_streak(session) == 3


def test_tool_failure_streak_counts_consecutive_failures() -> None:
    session = _session()
    session.append(_call(is_error=True))
    session.append(_call(is_error=True))
    session.append(_call(is_error=True))
    assert tool_failure_streak(session) == 3


def test_tool_failure_streak_resets_on_success() -> None:
    session = _session()
    session.append(_call(is_error=True))
    session.append(_call(is_error=True))
    session.append(_call(is_error=False))
    assert tool_failure_streak(session) == 0


def test_intervening_message_breaks_repeated_action_streak() -> None:
    session = _session()
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(UserMessageRecorded(content="hi"))
    session.append(_call(name="shell", arguments={"command": "a"}))
    assert repeated_action_streak(session) == 1


def test_intervening_user_message_breaks_failure_streak() -> None:
    session = _session()
    session.append(_call(is_error=True))
    session.append(UserMessageRecorded(content="hi"))
    session.append(_call(is_error=True))
    assert tool_failure_streak(session) == 1


def test_assistant_message_between_tool_calls_does_not_break_streak() -> None:
    session = _session()
    session.append(AssistantMessageRecorded(content="", thinking=""))
    session.append(_call(name="shell", arguments={"command": "a"}))
    session.append(AssistantMessageRecorded(content="", thinking=""))
    session.append(_call(name="shell", arguments={"command": "a"}))
    assert repeated_action_streak(session) == 2


def test_assess_below_thresholds_has_no_nudge_and_not_stuck() -> None:
    session = _session()
    session.append(_call(name="shell", arguments={"command": "a"}))
    result = assess(session)
    assert result == Stuckness(
        repeated_action_streak=1, tool_failure_streak=0, nudge=None, stuck=False
    )


def test_assess_repeated_action_nudge_threshold() -> None:
    session = _session()
    for _ in range(NUDGE_THRESHOLD):
        session.append(_call(name="shell", arguments={"command": "a"}))
    result = assess(session)
    assert result.nudge is not None
    assert "repeated" in result.nudge or "same action" in result.nudge
    assert str(NUDGE_THRESHOLD) in result.nudge
    assert result.stuck is False


def test_assess_failure_nudge_threshold() -> None:
    session = _session()
    for i in range(NUDGE_THRESHOLD):
        session.append(_call(arguments={"command": str(i)}, is_error=True))
    result = assess(session)
    assert result.nudge is not None
    assert "fail" in result.nudge
    assert result.stuck is False


def test_assess_stuck_when_repeated_action_streak_reaches_threshold() -> None:
    session = _session()
    for _ in range(STUCK_THRESHOLD):
        session.append(_call(name="shell", arguments={"command": "a"}))
    result = assess(session)
    assert result.stuck is True


def test_assess_stuck_when_failure_streak_reaches_threshold() -> None:
    session = _session()
    for i in range(STUCK_THRESHOLD):
        session.append(_call(arguments={"command": str(i)}, is_error=True))
    result = assess(session)
    assert result.stuck is True


def test_assess_prefers_repeated_action_wording_when_both_cross_nudge_threshold() -> None:
    session = _session()
    for _ in range(NUDGE_THRESHOLD):
        session.append(_call(name="shell", arguments={"command": "a"}, is_error=True))
    result = assess(session)
    assert result.nudge is not None
    assert "repeated" in result.nudge or "same action" in result.nudge


def test_windowed_repeat_none_on_empty_session() -> None:
    assert windowed_repeat(_session()) is None


def test_windowed_repeat_counts_non_consecutive_identical_calls() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    session.append(_call(name="read_file", arguments={"path": "b"}))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.call.name == "read_file"
    assert repeat.call.arguments == {"path": "a"}
    assert repeat.count == 2


def test_windowed_repeat_resets_on_new_user_message() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    session.append(UserMessageRecorded(content="hi"))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.count == 1


def test_windowed_repeat_ignores_non_identical_calls() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    session.append(_call(name="read_file", arguments={"path": "b"}))
    session.append(_call(name="shell", arguments={"command": "a"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.count == 1


def test_assess_windowed_repeat_nudge_names_existing_fact_id() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    session.append(_call(name="read_file", arguments={"path": "b"}))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    result = assess(session)
    assert result.nudge is not None
    assert "read_file(a)" in result.nudge
    assert "fact 1" in result.nudge
    assert result.stuck is False


def test_assess_windowed_repeat_with_missing_fact_yields_no_nudge_and_no_failure() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    session.append(_call(name="read_file", arguments={"path": "b"}))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    session.connection.execute("UPDATE events SET fact_id = NULL")
    result = assess(session)
    assert result.nudge is None
    assert result.stuck is False


def test_windowed_repeat_ignores_errored_calls() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}, is_error=True))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.count == 1


def test_assess_errored_call_then_identical_success_draws_no_repeat_nudge() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}, is_error=True))
    session.append(_call(name="read_file", arguments={"path": "b"}))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    result = assess(session)
    assert result.nudge is None
    assert result.stuck is False


def test_windowed_repeat_exempts_bookkeeping_tools() -> None:
    session = _session()
    for _ in range(STUCK_THRESHOLD):
        session.append(_call(name="read_fact", arguments={"id": 27}))
        session.append(_call(name="shell", arguments={"command": "ls"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.call.name == "shell"


def test_assess_repeated_recall_neither_nudges_nor_stops() -> None:
    session = _session()
    for i in range(STUCK_THRESHOLD):
        session.append(_call(name="read_fact", arguments={"id": 27}))
        session.append(_call(name="shell", arguments={"command": str(i)}))
    result = assess(session)
    assert result.nudge is None
    assert result.stuck is False


def test_assess_windowed_repeat_never_stops_the_run() -> None:
    session = _session()
    for i in range(STUCK_THRESHOLD):
        session.append(_call(name="read_file", arguments={"path": "a"}))
        session.append(_call(name="read_file", arguments={"path": str(i)}))
    result = assess(session)
    assert result.stuck is False
    assert result.nudge is not None
    assert "read_file(a)" in result.nudge
    assert "fact 1" in result.nudge


def test_assess_consecutive_repeats_still_stop_the_run() -> None:
    session = _session()
    for _ in range(STUCK_THRESHOLD):
        session.append(_call(name="read_file", arguments={"path": "a"}))
    result = assess(session)
    assert result.stuck is True
    assert result.reason is not None
    assert "same action" in result.reason


def test_streak_stop_survives_one_generation_per_tool_call() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="go"))
    for _ in range(STUCK_THRESHOLD):
        session.append(AssistantMessageRecorded(content="", thinking=""))
        session.append(_call(name="shell", arguments={"command": "a"}))
    result = assess(session)
    assert result.repeated_action_streak == STUCK_THRESHOLD
    assert result.stuck is True


def test_windowed_repeat_window_ends_after_window_generations() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    for _ in range(WINDOW_GENERATIONS):
        session.append(AssistantMessageRecorded(content="", thinking=""))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.count == 1


def test_windowed_repeat_counts_pair_within_the_generation_window() -> None:
    session = _session()
    session.append(_call(name="read_file", arguments={"path": "a"}))
    for _ in range(WINDOW_GENERATIONS - 1):
        session.append(AssistantMessageRecorded(content="", thinking=""))
    session.append(_call(name="read_file", arguments={"path": "a"}))
    repeat = windowed_repeat(session)
    assert repeat is not None
    assert repeat.count == 2


def test_assess_windowed_repeat_never_fires_for_distinct_calls() -> None:
    session = _session()
    for i in range(STUCK_THRESHOLD + 2):
        session.append(_call(name="read_file", arguments={"path": str(i)}))
    result = assess(session)
    assert result.stuck is False
    assert result.nudge is None


def test_generations_since_plan_event_counts_assistant_messages_after_last_plan_event() -> None:
    session = _session()
    session.append(PlanSet(steps=("one", "two")))
    session.append(AssistantMessageRecorded(content="a", thinking=""))
    session.append(AssistantMessageRecorded(content="b", thinking=""))
    assert generations_since_plan_event(session) == 2


def test_generations_since_plan_event_resets_on_complete_step() -> None:
    session = _session()
    session.append(PlanSet(steps=("one", "two")))
    session.append(AssistantMessageRecorded(content="a", thinking=""))
    session.append(PlanStepCompleted(index=0))
    session.append(AssistantMessageRecorded(content="b", thinking=""))
    assert generations_since_plan_event(session) == 1


def test_generations_since_plan_event_counts_from_start_without_a_plan_event() -> None:
    session = _session()
    session.append(AssistantMessageRecorded(content="a", thinking=""))
    session.append(AssistantMessageRecorded(content="b", thinking=""))
    assert generations_since_plan_event(session) == 2


def test_assess_nudges_stalled_plan_quoting_first_unfinished_step() -> None:
    session = _session()
    session.append(PlanSet(steps=("do the thing", "do another thing")))
    for _ in range(PLAN_STALL_GENERATIONS):
        session.append(AssistantMessageRecorded(content="thinking", thinking=""))
    result = assess(session)
    assert result.nudge is not None
    assert "do the thing" in result.nudge
    assert result.stuck is False


def test_assess_plan_stall_cleared_by_complete_step() -> None:
    session = _session()
    session.append(PlanSet(steps=("do the thing", "do another thing")))
    for _ in range(PLAN_STALL_GENERATIONS):
        session.append(AssistantMessageRecorded(content="thinking", thinking=""))
    session.append(PlanStepCompleted(index=0))
    result = assess(session)
    assert result.nudge is None


def test_assess_plan_stall_never_fires_when_all_steps_done() -> None:
    session = _session()
    session.append(PlanSet(steps=("do the thing",)))
    session.append(PlanStepCompleted(index=0))
    for _ in range(PLAN_STALL_GENERATIONS + 2):
        session.append(AssistantMessageRecorded(content="thinking", thinking=""))
    result = assess(session)
    assert result.nudge is None


def test_assess_plan_stall_below_threshold_does_not_nudge() -> None:
    session = _session()
    session.append(PlanSet(steps=("do the thing",)))
    for _ in range(PLAN_STALL_GENERATIONS - 1):
        session.append(AssistantMessageRecorded(content="thinking", thinking=""))
    result = assess(session)
    assert result.nudge is None


def test_assess_streak_nudge_takes_priority_over_plan_stall() -> None:
    session = _session()
    session.append(PlanSet(steps=("do the thing",)))
    for _ in range(PLAN_STALL_GENERATIONS):
        session.append(AssistantMessageRecorded(content="thinking", thinking=""))
    for _ in range(NUDGE_THRESHOLD):
        session.append(_call(name="shell", arguments={"command": "a"}))
    result = assess(session)
    assert result.nudge is not None
    assert "same action" in result.nudge
