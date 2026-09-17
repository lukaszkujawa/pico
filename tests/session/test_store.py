import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from pico.session import Session, ToolCallRecorded, UserMessageRecorded
from pico.session.store import connect

OLD_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_events_session_seq ON events (session_id, seq);
"""


def _insert_old_event(
    conn: sqlite3.Connection, session_id: str, seq: int, kind: str, payload: Mapping[str, object]
) -> None:
    conn.execute(
        "INSERT INTO events (session_id, seq, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, seq, kind, json.dumps(payload), "2026-01-01T00:00:00+00:00"),
    )


def test_connect_creates_events_table_and_index() -> None:
    conn = connect(":memory:")

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }

    assert "events" in tables
    assert "idx_events_session_seq_unique" in indexes


def test_connect_is_idempotent_on_same_path(tmp_path: Path) -> None:
    db_path = tmp_path / "session.db"

    connect(db_path)
    conn = connect(db_path)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "events" in tables


def test_connect_migrates_old_schema_and_backfills_fact_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "session.db"
    old = sqlite3.connect(db_path)
    old.executescript(OLD_SCHEMA)
    call = {"name": "shell", "arguments": {"command": "ls"}, "result": "ok", "is_error": False}
    _insert_old_event(old, "run-a", 1, "UserMessageRecorded", {"content": "task"})
    _insert_old_event(old, "run-a", 2, "ToolCallRecorded", call)
    _insert_old_event(old, "run-a/delegate/3", 1, "ToolCallRecorded", call)
    _insert_old_event(old, "run-b", 1, "ToolCallRecorded", call)
    old.commit()
    old.close()

    conn = connect(db_path)

    rows = conn.execute(
        "SELECT session_id, fact_id FROM events WHERE kind = 'ToolCallRecorded' ORDER BY id"
    ).fetchall()
    assert [(row["session_id"], row["fact_id"]) for row in rows] == [
        ("run-a", 1),
        ("run-a/delegate/3", 2),
        ("run-b", 1),
    ]

    session = Session(conn, "run-a")
    session.append(UserMessageRecorded(content="again"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="ok", is_error=False))
    assert [fact_id for fact_id, _ in session.tree_records()] == [1, 2, 3]
