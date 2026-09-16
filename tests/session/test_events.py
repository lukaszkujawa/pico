from pico.session.events import (
    AssistantMessageRecorded,
    PlanSet,
    PlanStepCompleted,
    SessionEvent,
    ToolCallRecorded,
    UserMessageRecorded,
)


def test_user_message_recorded_construction() -> None:
    event = UserMessageRecorded(content="hi")
    assert event.content == "hi"


def test_assistant_message_recorded_construction() -> None:
    event = AssistantMessageRecorded(content="hello", thinking="pondering")
    assert event.content == "hello"
    assert event.thinking == "pondering"


def test_tool_call_recorded_construction() -> None:
    event = ToolCallRecorded(name="echo", arguments={"text": "hi"}, result="hi", is_error=False)
    assert event.name == "echo"
    assert event.arguments == {"text": "hi"}
    assert event.result == "hi"
    assert event.is_error is False


def test_plan_set_construction() -> None:
    event = PlanSet(steps=("read", "write"))
    assert event.steps == ("read", "write")


def test_plan_step_completed_construction() -> None:
    event = PlanStepCompleted(index=1)
    assert event.index == 1


def test_session_event_membership() -> None:
    events: list[SessionEvent] = [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="hello", thinking=""),
        ToolCallRecorded(name="echo", arguments={}, result="ok", is_error=False),
        PlanSet(steps=("one",)),
        PlanStepCompleted(index=0),
    ]
    assert all(
        isinstance(
            event,
            UserMessageRecorded
            | AssistantMessageRecorded
            | ToolCallRecorded
            | PlanSet
            | PlanStepCompleted,
        )
        for event in events
    )
