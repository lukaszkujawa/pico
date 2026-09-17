from pathlib import Path

from pico.session.store import connect


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
