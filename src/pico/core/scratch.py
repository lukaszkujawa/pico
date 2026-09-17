import csv
import re
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

from pico.core.errors import ToolError
from pico.session import Session

MAX_ROWS = 50
QUERY_TIMEOUT_SECONDS = 5.0
_PROGRESS_INSTRUCTIONS = 20_000


def scratch_path(session: Session) -> str:
    row = session.connection.execute("PRAGMA database_list").fetchone()
    main = "" if row is None else str(row["file"])
    if not main:
        return ":memory:"
    return str(Path(main).parent / f"scratch-{session.session_id.replace('/', '-')}.db")


class Scratch:
    def __init__(self, session: Session) -> None:
        self._path = scratch_path(session)
        self._conn: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
        return self._conn


def identifier(name: str, fallback: str) -> str:
    cleaned = re.sub(r"\W+", "_", name.strip()).strip("_").lower()
    if not cleaned:
        return fallback
    return f"c_{cleaned}" if cleaned[0].isdigit() else cleaned


def _columns(header: Sequence[str]) -> list[str]:
    taken: list[str] = []
    for position, name in enumerate(header):
        column = identifier(name, f"column_{position}")
        candidate, suffix = column, 2
        while candidate in taken:
            candidate, suffix = f"{column}_{suffix}", suffix + 1
        taken.append(candidate)
    return taken


def _number(value: str) -> float | int | None:
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None


def _numeric(values: Sequence[str]) -> bool:
    present = [value.strip() for value in values if value.strip()]
    return bool(present) and all(_number(value) is not None for value in present)


def _cell(value: str, numeric: bool) -> object:
    if not numeric:
        return value
    return None if not value.strip() else _number(value.strip())


def _read_csv(path: str) -> tuple[list[str], list[list[str]]]:
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ToolError(f"could not read {path}: {error}") from error
    if not rows:
        raise ToolError(f"{path} is empty; the first row must be a header")
    header, *data = rows
    if not any(name.strip() for name in header):
        raise ToolError(f"{path} has an empty header row")
    return header, data


def load_table(scratch: Scratch, path: str, table: str) -> str:
    name = identifier(table, "scratch")
    header, data = _read_csv(path)
    columns = _columns(header)
    grid = [
        [row[index] if index < len(row) else "" for index in range(len(columns))] for row in data
    ]
    numeric = [_numeric([row[index] for row in grid]) for index in range(len(columns))]
    types = ["NUMERIC" if flag else "TEXT" for flag in numeric]
    definition = ", ".join(f'"{c}" {t}' for c, t in zip(columns, types, strict=True))
    values = [
        [_cell(value, flag) for value, flag in zip(row, numeric, strict=True)] for row in grid
    ]
    conn = scratch.connection
    with conn:
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
        conn.execute(f'CREATE TABLE "{name}" ({definition})')
        conn.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(columns))})', values)
    return f"table {name}({', '.join(columns)}) loaded with {len(values)} rows"


def render(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    shown = [["" if value is None else str(value) for value in row] for row in rows[:MAX_ROWS]]
    widths = [max(len(cell) for cell in column) for column in zip(columns, *shown, strict=True)]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in [list(columns), *shown]
    ]
    if len(rows) > MAX_ROWS:
        lines.append(f"+{len(rows) - MAX_ROWS} more rows")
    return "\n".join(lines)


def query(scratch: Scratch, statement: str) -> str:
    conn = scratch.connection
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    conn.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_INSTRUCTIONS)
    try:
        with conn:
            cursor = conn.execute(statement)
            rows = cursor.fetchall()
            description = cursor.description
    except sqlite3.Error as error:
        if time.monotonic() > deadline:
            raise ToolError(f"query timed out after {QUERY_TIMEOUT_SECONDS}s") from error
        raise ToolError(str(error)) from error
    finally:
        conn.set_progress_handler(None, _PROGRESS_INSTRUCTIONS)
    if description is None:
        return "ok"
    if not rows:
        return "no rows"
    return render([str(column[0]) for column in description], rows)
