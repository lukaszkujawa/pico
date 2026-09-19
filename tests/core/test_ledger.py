from pathlib import Path

from pico.core.ledger import Fact, Plan, PlanStep, fact_index, facts, plan, render_call
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


def _fact(
    fact_id: int, content: str, source: str = "shell", arguments: dict[str, object] | None = None
) -> Fact:
    return Fact(id=fact_id, content=content, source=source, arguments=arguments or {})


def test_fact_index_lists_every_fact_with_id_and_signature() -> None:
    index = fact_index(
        [
            _fact(1, "alpha", "shell", {"command": "ls"}),
            _fact(4, "beta", "read_file", {"path": "a.py"}),
        ]
    )
    lines = index.splitlines()

    assert lines[0] == "[1] shell(ls): alpha"
    assert lines[1] == "[4] read_file(a.py): beta"


def test_fact_index_distinguishes_facts_by_signature_within_the_line_head() -> None:
    index = fact_index(
        [
            _fact(1, "same preview", "read_file", {"path": "src/pico/core/loop.py"}),
            _fact(2, "same preview", "read_file", {"path": "src/pico/core/prompt.py"}),
        ]
    )
    first, second = index.splitlines()[:2]

    assert first[:40] != second[:40]


def test_fact_index_shell_signature_shows_the_leading_part_of_the_command() -> None:
    index = fact_index(
        [_fact(1, "output", "shell", {"command": "grep -rn TODO src/pico/core/loop.py"})]
    )

    assert "grep -rn TODO" in index.splitlines()[0]


def test_fact_index_collapses_multiline_content_to_a_bounded_preview() -> None:
    content = "first line\nsecond line\n" + "x" * 500
    index = fact_index([_fact(2, content)])
    line = index.splitlines()[0]

    assert "\n" not in line
    assert "first line second line" in line
    assert len(line) < 95


def test_fact_index_line_stays_within_the_bound_for_a_wide_signature() -> None:
    command = "cd /tmp/pico2 && uv run pytest tests/core/test_loop.py | tail -20"
    index = fact_index([_fact(12345, "x" * 500, "shell", {"command": command})])
    line = index.splitlines()[0]

    assert len(line) <= 90
    assert "\n" not in line
    assert "tail -20" in line


def test_fact_index_caps_at_the_most_recent_facts_with_overflow_line() -> None:
    all_facts = [
        _fact(fact_id, f"content {fact_id}", arguments={"command": str(fact_id)})
        for fact_id in range(30)
    ]
    lines = fact_index(all_facts).splitlines()

    listed_ids = [int(line[1 : line.index("]")]) for line in lines if line.startswith("[")]
    assert listed_ids == list(range(10, 30))
    assert "+10 earlier facts" in lines


def test_fact_index_keeps_only_the_newest_fact_per_producing_call() -> None:
    args: dict[str, object] = {"path": "loop.py"}
    lines = fact_index(
        [
            _fact(1, "old", "read_file", args),
            _fact(2, "older", "read_file", args),
            _fact(3, "newest", "read_file", args),
        ]
    ).splitlines()

    assert lines[0] == "[3] read_file(loop.py): newest"
    assert not any(line.startswith("[1]") or line.startswith("[2]") for line in lines)


def test_facts_deduped_from_the_index_remain_recoverable_by_id() -> None:
    session = _session()
    for index in range(3):
        session.append(
            ToolCallRecorded(
                name="read_file",
                arguments={"path": "loop.py"},
                result=f"body {index}",
                is_error=False,
            )
        )
    all_facts = facts(session)
    index = fact_index(all_facts)

    assert [fact.id for fact in all_facts] == [1, 2, 3]
    assert "[3]" in index
    assert "[1]" not in index and "[2]" not in index


def test_fact_index_never_merges_distinct_calls() -> None:
    lines = fact_index(
        [
            _fact(1, "a", "read_file", {"path": "loop.py"}),
            _fact(2, "b", "read_file", {"path": "prompt.py"}),
            _fact(3, "c", "shell", {"command": "loop.py"}),
        ]
    ).splitlines()

    listed_ids = [int(line[1 : line.index("]")]) for line in lines if line.startswith("[")]
    assert listed_ids == [1, 2, 3]


def test_fact_index_overflow_counts_only_facts_hidden_by_the_cut() -> None:
    duplicates = [_fact(fact_id, "dup", "read_file", {"path": "loop.py"}) for fact_id in range(10)]
    distinct = [
        _fact(100 + index, "content", "shell", {"command": str(index)}) for index in range(25)
    ]
    lines = fact_index([*duplicates, *distinct]).splitlines()

    listed = [line for line in lines if line.startswith("[")]
    assert len(listed) == 20
    assert "+6 earlier facts" in lines
    assert len(lines) == 22


def test_fact_index_of_no_facts_is_empty() -> None:
    assert fact_index([]) == ""


def test_fact_index_includes_the_recovery_hint_whenever_facts_are_listed() -> None:
    assert "read_fact(" in fact_index([_fact(7, "something")])
