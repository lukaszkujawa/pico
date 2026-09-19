import pytest

from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session.events import (
    AssistantMessageRecorded,
    PlanSet,
    PlanStepCompleted,
    ToolCallRecorded,
    UserMessageRecorded,
)
from pico.session.session import (
    ELIDE_CONTENT_CHARS,
    Session,
    UnknownEventKindError,
    latest_session_id,
    new_session_id,
)
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
    assert messages[1].role == Role.ASSISTANT
    assert messages[1].tool_calls == (
        ToolCall(id=messages[1].tool_calls[0].id, name="echo", arguments={"text": "hi"}),
    )
    assert messages[2] == Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id=messages[1].tool_calls[0].id, content="hi", is_error=False, name="echo"
        ),
    )
    assert messages[3] == Message(role=Role.ASSISTANT, content="done")


def test_messages_skips_thinking_only_assistant_events() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="hi"))
    session.append(AssistantMessageRecorded(content="", thinking="pondering"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="hi", is_error=False))
    session.append(AssistantMessageRecorded(content="", thinking="more pondering"))
    session.append(AssistantMessageRecorded(content="done", thinking=""))

    messages = session.messages()

    assert list(session.events())[1] == AssistantMessageRecorded(content="", thinking="pondering")
    assert not any(
        m.role is Role.ASSISTANT and not m.content and not m.tool_calls for m in messages
    )
    assert [m.content for m in messages if m.role is Role.ASSISTANT and m.content] == ["done"]


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


def test_records_pairs_each_event_with_its_fact_id_or_none() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="one"))
    session.append(AssistantMessageRecorded(content="two", thinking=""))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="three", is_error=False))

    records = list(session.records())

    assert records == [
        (None, UserMessageRecorded(content="one")),
        (None, AssistantMessageRecorded(content="two", thinking="")),
        (1, ToolCallRecorded(name="echo", arguments={}, result="three", is_error=False)),
    ]


def test_events_matches_records_events() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(UserMessageRecorded(content="one"))
    session.append(AssistantMessageRecorded(content="two", thinking=""))

    assert list(session.events()) == [event for _, event in session.records()]


def test_messages_uses_the_fact_id_as_tool_call_id() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(ToolCallRecorded(name="echo", arguments={}, result="first", is_error=False))
    session.append(AssistantMessageRecorded(content="thinking out loud", thinking=""))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="second", is_error=False))

    messages = session.messages()
    tool_results = [m.tool_result for m in messages if m.role is Role.TOOL]
    tool_calls = [call for m in messages for call in m.tool_calls]

    assert [call.id for call in tool_calls] == ["1", "2"]
    assert [result.tool_call_id for result in tool_results if result is not None] == ["1", "2"]


def test_child_session_transcript_is_independent_of_parent() -> None:
    conn = connect(":memory:")
    parent = Session(conn, "parent")
    parent.append(UserMessageRecorded(content="a"))
    parent.append(UserMessageRecorded(content="b"))
    child = parent.child("delegate/1")
    child.append(UserMessageRecorded(content="c"))

    assert list(parent.events()) == [
        UserMessageRecorded(content="a"),
        UserMessageRecorded(content="b"),
    ]
    assert list(child.events()) == [UserMessageRecorded(content="c")]


def test_messages_elides_write_file_content_over_threshold() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")
    content = "x" * (ELIDE_CONTENT_CHARS + 1)
    session.append(
        ToolCallRecorded(
            name="write_file",
            arguments={"path": "/app/gsearch.py", "content": content},
            result="ok",
            is_error=False,
        )
    )

    call = session.messages()[0].tool_calls[0]

    assert call.arguments == {
        "path": "/app/gsearch.py",
        "content": f"<{len(content)} chars — on disk at /app/gsearch.py; read_file to recover>",
    }
    event = next(session.events())
    assert isinstance(event, ToolCallRecorded)
    assert event.arguments["content"] == content


def test_messages_keeps_write_file_content_at_threshold() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")
    content = "y" * ELIDE_CONTENT_CHARS
    session.append(
        ToolCallRecorded(
            name="write_file",
            arguments={"path": "/app/small.py", "content": content},
            result="ok",
            is_error=False,
        )
    )

    call = session.messages()[0].tool_calls[0]

    assert call.arguments == {"path": "/app/small.py", "content": content}


def test_write_file_rendering_is_identical_as_session_grows() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")
    session.append(
        ToolCallRecorded(
            name="write_file",
            arguments={"path": "/app/big.py", "content": "z" * (ELIDE_CONTENT_CHARS * 3)},
            result="ok",
            is_error=False,
        )
    )
    short = session.messages()[:2]

    session.append(UserMessageRecorded(content="carry on"))
    session.append(ToolCallRecorded(name="echo", arguments={}, result="hi", is_error=False))

    assert session.messages()[:2] == short


def test_plan_events_round_trip_through_append_and_events() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")

    session.append(PlanSet(steps=("read the file", "write the answer")))
    session.append(PlanStepCompleted(index=0))

    assert list(session.events()) == [
        PlanSet(steps=("read the file", "write the answer")),
        PlanStepCompleted(index=0),
    ]


def test_messages_ignores_plan_events() -> None:
    conn = connect(":memory:")
    plain = Session(conn, "plain")
    planned = Session(conn, "planned")

    for session in (plain, planned):
        session.append(UserMessageRecorded(content="hi"))
        if session is planned:
            session.append(PlanSet(steps=("one", "two")))
            session.append(PlanStepCompleted(index=1))
        session.append(AssistantMessageRecorded(content="done", thinking=""))

    assert planned.messages() == plain.messages()
