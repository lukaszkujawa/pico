from collections import Counter
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from typing import cast

from pico.core.ledger import facts, plan, render_call
from pico.session import (
    AssistantMessageRecorded,
    PlanSet,
    PlanStepCompleted,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
)

NUDGE_THRESHOLD = 2
STUCK_THRESHOLD = 6
PLAN_STALL_GENERATIONS = 6


@dataclass(frozen=True)
class Stuckness:
    repeated_action_streak: int
    tool_failure_streak: int
    nudge: str | None
    stuck: bool
    reason: str | None = None


def _trailing_tool_calls(session: Session) -> list[ToolCallRecorded]:
    result: list[ToolCallRecorded] = []
    for event in reversed(list(session.events())):
        if isinstance(event, UserMessageRecorded):
            break
        if isinstance(event, ToolCallRecorded):
            result.append(event)
    return result


def _turn_tool_calls(session: Session) -> list[ToolCallRecorded]:
    return list(reversed(_trailing_tool_calls(session)))


def _freeze(value: object) -> Hashable:
    if isinstance(value, Mapping):
        items = cast("Mapping[str, object]", value).items()
        return tuple(sorted((key, _freeze(item)) for key, item in items))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in cast("list[object]", value))
    return cast(Hashable, value)


@dataclass(frozen=True)
class _WindowedRepeat:
    call: ToolCallRecorded
    count: int


def windowed_repeat(session: Session) -> _WindowedRepeat | None:
    calls = _turn_tool_calls(session)
    if not calls:
        return None
    counts = Counter((call.name, _freeze(call.arguments)) for call in calls)
    key, count = counts.most_common(1)[0]
    call = next(call for call in calls if (call.name, _freeze(call.arguments)) == key)
    return _WindowedRepeat(call=call, count=count)


def _existing_fact_id(session: Session, call: ToolCallRecorded) -> int | None:
    for fact in facts(session):
        if fact.source == call.name and fact.arguments == call.arguments:
            return fact.id
    return None


def generations_since_plan_event(session: Session) -> int:
    count = 0
    for event in reversed(list(session.events())):
        if isinstance(event, (PlanSet, PlanStepCompleted)):
            return count
        if isinstance(event, AssistantMessageRecorded):
            count += 1
    return count


def plan_stall_nudge(session: Session) -> str | None:
    current = plan(session)
    if current is None or all(step.done for step in current.steps):
        return None
    if generations_since_plan_event(session) < PLAN_STALL_GENERATIONS:
        return None
    first_unfinished = next(step for step in current.steps if not step.done)
    return (
        f"the plan has been stalled for {PLAN_STALL_GENERATIONS} generations — "
        f"complete_step the one you've finished or set_plan to revise it. "
        f'first unfinished step: "{first_unfinished.text}"'
    )


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


def _windowed_nudge(session: Session, repeat: _WindowedRepeat) -> str:
    signature = render_call(repeat.call.name, repeat.call.arguments)
    fact_id = _existing_fact_id(session, repeat.call)
    if fact_id is None:
        return (
            f"you already ran {signature} {repeat.count} times this turn "
            "— do something new instead of repeating it"
        )
    return (
        f"you already ran {signature} — its result is fact {fact_id}; "
        f"use read_fact({fact_id}) or do something new"
    )


def assess(session: Session) -> Stuckness:
    repeated = repeated_action_streak(session)
    failures = tool_failure_streak(session)
    repeat = windowed_repeat(session)
    windowed_count = 0 if repeat is None else repeat.count

    reason: str | None = None
    if repeated >= STUCK_THRESHOLD:
        reason = f"repeated the same action {repeated} times"
    elif failures >= STUCK_THRESHOLD:
        reason = f"{failures} tool calls failed in a row"
    elif repeat is not None and windowed_count >= STUCK_THRESHOLD:
        signature = render_call(repeat.call.name, repeat.call.arguments)
        reason = f"repeated {signature} {windowed_count} times this turn"

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
    elif repeat is not None and windowed_count >= NUDGE_THRESHOLD:
        nudge = _windowed_nudge(session, repeat)
    else:
        nudge = plan_stall_nudge(session)

    return Stuckness(
        repeated_action_streak=repeated,
        tool_failure_streak=failures,
        nudge=nudge,
        stuck=reason is not None,
        reason=reason,
    )
