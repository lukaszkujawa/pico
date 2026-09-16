from pico.core.ledger import Fact, Plan, PlanStep, facts, plan
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


def test_facts_empty_session_returns_empty_list() -> None:
    assert facts(_session()) == []


def test_facts_skips_error_tool_calls() -> None:
    session = _session()
    session.append(
        ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True),
    )

    assert facts(session) == []


def test_facts_ids_are_source_event_seqs() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="do it"))
    session.append(AssistantMessageRecorded(content="", thinking=""))
    session.append(
        ToolCallRecorded(
            name="read_file", arguments={"path": "a"}, result="a-content", is_error=False
        )
    )
    session.append(
        ToolCallRecorded(name="shell", arguments={}, result="bad", is_error=True),
    )
    session.append(
        ToolCallRecorded(name="shell", arguments={"command": "x"}, result="ok", is_error=False)
    )

    assert facts(session) == [
        Fact(id=3, content="a-content", source="read_file"),
        Fact(id=5, content="ok", source="shell"),
    ]


def test_facts_ids_are_unchanged_by_interleaved_error_calls() -> None:
    session = _session()
    session.append(ToolCallRecorded(name="shell", arguments={}, result="first", is_error=False))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="last", is_error=False))

    assert [fact.id for fact in facts(session)] == [1, 3]


def test_plan_returns_none_without_plan_events() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="do it"))

    assert plan(session) is None


def test_plan_marks_completed_steps_done() -> None:
    session = _session()
    session.append(PlanSet(steps=("one", "two", "three")))
    session.append(PlanStepCompleted(index=0))
    session.append(PlanStepCompleted(index=2))

    assert plan(session) == Plan(
        steps=(
            PlanStep(text="one", done=True),
            PlanStep(text="two", done=False),
            PlanStep(text="three", done=True),
        )
    )


def test_plan_reset_discards_prior_completion() -> None:
    session = _session()
    session.append(PlanSet(steps=("one", "two")))
    session.append(PlanStepCompleted(index=0))
    session.append(PlanSet(steps=("fresh", "start")))

    assert plan(session) == Plan(
        steps=(PlanStep(text="fresh", done=False), PlanStep(text="start", done=False))
    )


def test_completion_of_superseded_plan_does_not_corrupt_current_plan() -> None:
    session = _session()
    session.append(PlanSet(steps=("a", "b", "c", "d")))
    session.append(PlanStepCompleted(index=3))
    session.append(PlanSet(steps=("only",)))
    session.append(PlanStepCompleted(index=3))

    assert plan(session) == Plan(steps=(PlanStep(text="only", done=False),))
