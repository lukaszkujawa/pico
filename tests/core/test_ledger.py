from pico.core.ledger import Fact, Goal, facts, goal
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


def test_goal_returns_none_when_no_user_messages() -> None:
    session = _session()
    session.append(AssistantMessageRecorded(content="hi", thinking=""))

    assert goal(session) is None


def test_goal_returns_latest_user_message() -> None:
    session = _session()
    session.append(UserMessageRecorded(content="first"))
    session.append(AssistantMessageRecorded(content="ok", thinking=""))
    session.append(UserMessageRecorded(content="second"))

    assert goal(session) == Goal(content="second")
