import pytest

from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session.errors import UnknownEventKindError
from pico.session.events import AssistantMessageRecorded, ToolCallRecorded, UserMessageRecorded
from pico.session.session import Session, latest_session_id, new_session_id
from pico.session.store import connect


def test_append_and_events_preserves_order_and_values() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="hi"))
    session.append(AssistantMessageRecorded(content="hello", thinking="pondering"))
    session.append(
        ToolCallRecorded(name="echo", arguments={"text": "hi"}, result="hi", is_error=False)
    )

    events = list(session.events())

    assert events == [
        UserMessageRecorded(content="hi"),
        AssistantMessageRecorded(content="hello", thinking="pondering"),
        ToolCallRecorded(name="echo", arguments={"text": "hi"}, result="hi", is_error=False),
    ]


def test_messages_derives_expected_shape() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="hi"))
    session.append(AssistantMessageRecorded(content="", thinking=""))
    session.append(
        ToolCallRecorded(name="echo", arguments={"text": "hi"}, result="hi", is_error=False)
    )
    session.append(AssistantMessageRecorded(content="done", thinking=""))

    messages = session.messages()

    assert messages[0] == Message(role=Role.USER, content="hi")
    assert messages[1] == Message(role=Role.ASSISTANT, content="")
    assert messages[2].role == Role.ASSISTANT
    assert messages[2].tool_calls == (
        ToolCall(id=messages[2].tool_calls[0].id, name="echo", arguments={"text": "hi"}),
    )
    assert messages[3] == Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id=messages[2].tool_calls[0].id, content="hi", is_error=False
        ),
    )
    assert messages[4] == Message(role=Role.ASSISTANT, content="done")


def test_different_sessions_on_same_connection_are_isolated() -> None:
    conn = connect(":memory:")
    session_a = Session(conn, "a")
    session_b = Session(conn, "b")

    session_a.append(UserMessageRecorded(content="from a"))
    session_b.append(UserMessageRecorded(content="from b"))

    assert list(session_a.events()) == [UserMessageRecorded(content="from a")]
    assert list(session_b.events()) == [UserMessageRecorded(content="from b")]


def test_unknown_event_kind_raises_on_replay() -> None:
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO events (session_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        ("s1", 1, "MysteryEvent", "{}", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    session = Session(conn, "s1")

    with pytest.raises(UnknownEventKindError):
        list(session.events())


def test_seq_is_monotonic_per_session() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="one"))
    session.append(UserMessageRecorded(content="two"))

    rows = conn.execute(
        "SELECT seq FROM events WHERE session_id = ? ORDER BY seq", ("s1",)
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]


def test_new_session_id_is_unique_per_call() -> None:
    assert new_session_id() != new_session_id()


def test_latest_session_id_is_none_on_empty_database() -> None:
    assert latest_session_id(connect(":memory:")) is None


def test_latest_session_id_returns_most_recently_active_session() -> None:
    conn = connect(":memory:")
    Session(conn, "older").append(UserMessageRecorded(content="hello"))
    Session(conn, "newer").append(UserMessageRecorded(content="world"))
    Session(conn, "older").append(UserMessageRecorded(content="again"))

    assert latest_session_id(conn) == "older"


def test_latest_session_id_ignores_delegate_child_sessions() -> None:
    conn = connect(":memory:")
    parent = Session(conn, "parent")
    parent.append(UserMessageRecorded(content="hello"))
    parent.child("delegate/0").append(UserMessageRecorded(content="sub"))

    assert latest_session_id(conn) == "parent"


def test_records_yields_seqs_in_append_order_starting_at_one() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="one"))
    session.append(AssistantMessageRecorded(content="two", thinking=""))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="three", is_error=False))

    records = list(session.records())

    assert records == [
        (1, UserMessageRecorded(content="one")),
        (2, AssistantMessageRecorded(content="two", thinking="")),
        (3, ToolCallRecorded(name="echo", arguments={}, result="three", is_error=False)),
    ]


def test_events_matches_records_events() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="one"))
    session.append(AssistantMessageRecorded(content="two", thinking=""))

    assert list(session.events()) == [event for _, event in session.records()]


def test_messages_uses_event_seq_as_tool_call_id() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(ToolCallRecorded(name="echo", arguments={}, result="first", is_error=False))
    session.append(AssistantMessageRecorded(content="thinking out loud", thinking=""))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="second", is_error=False))

    messages = session.messages()
    tool_results = [m.tool_result for m in messages if m.role is Role.TOOL]
    tool_calls = [call for m in messages for call in m.tool_calls]

    assert [call.id for call in tool_calls] == ["1", "3"]
    assert [result.tool_call_id for result in tool_results if result is not None] == ["1", "3"]


def test_child_session_seqs_are_independent_of_parent() -> None:
    conn = connect(":memory:")
    parent = Session(conn, "parent")
    parent.append(UserMessageRecorded(content="a"))
    parent.append(UserMessageRecorded(content="b"))
    child = parent.child("delegate/1")
    child.append(UserMessageRecorded(content="c"))

    assert [seq for seq, _ in parent.records()] == [1, 2]
    assert [seq for seq, _ in child.records()] == [1]
