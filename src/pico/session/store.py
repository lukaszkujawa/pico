import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    fact_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

DROP INDEX IF EXISTS idx_events_session_seq;

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_session_seq_unique ON events (session_id, seq);
"""


def _migrate_fact_id(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "fact_id" in columns:
        return
    conn.execute("ALTER TABLE events ADD COLUMN fact_id INTEGER")
    counters: dict[str, int] = {}
    rows = conn.execute(
        "SELECT id, session_id FROM events WHERE kind = 'ToolCallRecorded' ORDER BY id"
    ).fetchall()
    for row in rows:
        root = str(row["session_id"]).split("/", 1)[0]
        counters[root] = counters.get(root, 0) + 1
        conn.execute("UPDATE events SET fact_id = ? WHERE id = ?", (counters[root], row["id"]))
    conn.commit()


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_fact_id(conn)
    conn.commit()
    return conn
