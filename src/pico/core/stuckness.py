from dataclasses import dataclass

from pico.session import Session, ToolCallRecorded, UserMessageRecorded

NUDGE_THRESHOLD = 2
STUCK_THRESHOLD = 6


@dataclass(frozen=True)
class Stuckness:
    repeated_action_streak: int
    tool_failure_streak: int
    nudge: str | None
    stuck: bool


def _trailing_tool_calls(session: Session) -> list[ToolCallRecorded]:
    result: list[ToolCallRecorded] = []
    for event in reversed(list(session.events())):
        if isinstance(event, UserMessageRecorded):
            break
        if isinstance(event, ToolCallRecorded):
            result.append(event)
    return result


def repeated_action_streak(session: Session) -> int:
    calls = _trailing_tool_calls(session)
    if not calls:
        return 0
    last = calls[0]
    streak = 0
    for call in calls:
        if call.name == last.name and call.arguments == last.arguments:
            streak += 1
        else:
            break
    return streak


def tool_failure_streak(session: Session) -> int:
    calls = _trailing_tool_calls(session)
    if not calls or not calls[0].is_error:
        return 0
    streak = 0
    for call in calls:
        if call.is_error:
            streak += 1
        else:
            break
    return streak


def assess(session: Session) -> Stuckness:
    repeated = repeated_action_streak(session)
    failures = tool_failure_streak(session)

    nudge: str | None = None
    if repeated >= NUDGE_THRESHOLD:
        nudge = (
            f"you've repeated the same action {repeated} times with no new result "
            "— try something different or use delegate/answer"
        )
    elif failures >= NUDGE_THRESHOLD:
        nudge = (
            f"the last {failures} tool calls failed "
            "— reconsider your approach instead of retrying the same way"
        )

    stuck = repeated >= STUCK_THRESHOLD or failures >= STUCK_THRESHOLD

    return Stuckness(
        repeated_action_streak=repeated,
        tool_failure_streak=failures,
        nudge=nudge,
        stuck=stuck,
    )
