from pathlib import Path

from pico.core.ledger import Fact, Plan, PlanStep, facts, plan, render_call
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


def test_facts_ids_number_fact_bearing_events_in_order() -> None:
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
        Fact(id=1, content="a-content", source="read_file", arguments={"path": "a"}),
        Fact(id=3, content="ok", source="shell", arguments={"command": "x"}),
    ]


def test_facts_ids_are_unchanged_by_interleaved_error_calls() -> None:
    session = _session()
    session.append(ToolCallRecorded(name="shell", arguments={}, result="first", is_error=False))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="last", is_error=False))

    assert [fact.id for fact in facts(session)] == [1, 3]


def test_facts_excludes_bookkeeping_tool_calls() -> None:
    session = _session()
    session.append(ToolCallRecorded(name="note", arguments={}, result="finding", is_error=False))
    session.append(
        ToolCallRecorded(name="read_fact", arguments={"id": 1}, result="finding", is_error=False)
    )
    session.append(
        ToolCallRecorded(name="search_facts", arguments={}, result="[1] note", is_error=False)
    )
    session.append(
        ToolCallRecorded(name="set_plan", arguments={}, result="[ ] 0. go", is_error=False)
    )
    session.append(
        ToolCallRecorded(name="complete_step", arguments={}, result="[x] 0. go", is_error=False)
    )
    session.append(ToolCallRecorded(name="shell", arguments={}, result="ok", is_error=False))

    assert facts(session) == [
        Fact(id=1, content="finding", source="note", arguments={}),
        Fact(id=6, content="ok", source="shell", arguments={}),
    ]


def test_recovering_a_fact_does_not_mint_a_new_fact() -> None:
    session = _session()
    session.append(ToolCallRecorded(name="note", arguments={}, result="finding", is_error=False))
    before = facts(session)
    session.append(
        ToolCallRecorded(name="read_fact", arguments={"id": 1}, result="finding", is_error=False)
    )

    assert facts(session) == before


def test_render_call_joins_argument_values_without_keys() -> None:
    assert render_call("read_file", {"path": "a.py"}) == "read_file(a.py)"


def test_render_call_with_no_arguments_shows_empty_parens() -> None:
    assert render_call("shell", {}) == "shell()"


def test_render_call_elides_the_middle_of_a_long_call_keeping_head_and_tail() -> None:
    rendered = render_call(
        "read_file", {"path": "src/pico/core/very/deeply/nested/and/even/longer/loop.py"}
    )

    assert len(rendered) <= 60
    assert rendered.startswith("read_file(")
    assert rendered.endswith("loop.py)")
    assert "…" in rendered


def test_render_call_keeps_the_distinguishing_tool_name_of_a_shell_command() -> None:
    vulture = render_call("shell", {"command": "cd /tmp/pico2 && uv run vulture 2>&1 | tail -10"})
    pyright = render_call("shell", {"command": "cd /tmp/pico2 && uv run pyright 2>&1 | tail -10"})

    assert "vulture" in vulture
    assert "pyright" in pyright
    assert vulture != pyright


def test_render_call_keeps_the_pipe_tail_of_an_over_long_command() -> None:
    rendered = render_call(
        "shell", {"command": "cd /tmp/pico2 && uv run pytest tests/core/test_loop.py | tail -20"}
    )

    assert len(rendered) <= 60
    assert rendered.endswith("tail -20)")
    assert "…" in rendered


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


def test_facts_span_the_whole_run_tree_in_id_order() -> None:
    session = _session()
    child = session.child("delegate/1")
    session.append(ToolCallRecorded(name="shell", arguments={}, result="parent", is_error=False))
    child.append(ToolCallRecorded(name="shell", arguments={}, result="child", is_error=False))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="after", is_error=False))

    assert [(fact.id, fact.content) for fact in facts(child)] == [
        (1, "parent"),
        (2, "child"),
        (3, "after"),
    ]
    assert facts(child) == facts(session)


def test_fact_ids_are_unique_across_nested_sessions() -> None:
    session = _session()
    grandchild = session.child("delegate/1").child("delegate/1")
    sibling = session.child("delegate/2")
    for node, result in ((session, "a"), (grandchild, "b"), (sibling, "c")):
        node.append(ToolCallRecorded(name="note", arguments={}, result=result, is_error=False))

    assert [fact.id for fact in facts(session)] == [1, 2, 3]


def test_facts_of_another_tree_are_not_visible() -> None:
    session = _session()
    other = Session(session.connection, "s2")
    session.append(ToolCallRecorded(name="note", arguments={}, result="mine", is_error=False))
    other.append(ToolCallRecorded(name="note", arguments={}, result="theirs", is_error=False))

    assert [fact.content for fact in facts(session)] == ["mine"]
    assert [fact.content for fact in facts(other)] == ["theirs"]


def test_fact_ids_are_stable_across_reopening_the_store(tmp_path: Path) -> None:
    conn = connect(tmp_path / "session.db")
    session = Session(conn, "s1")
    session.append(ToolCallRecorded(name="note", arguments={}, result="first", is_error=False))
    before = facts(session)

    reopened = Session(connect(tmp_path / "session.db"), "s1")
    reopened.append(ToolCallRecorded(name="note", arguments={}, result="second", is_error=False))

    assert facts(reopened)[: len(before)] == before
    assert [fact.id for fact in facts(reopened)] == [1, 2]
