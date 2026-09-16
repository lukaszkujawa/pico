from pico.core.stuckness import (
    NUDGE_THRESHOLD,
    STUCK_THRESHOLD,
    Stuckness,
    assess,
    repeated_action_streak,
    tool_failure_streak,
)
from pico.session import (
    AssistantMessageRecorded,
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
