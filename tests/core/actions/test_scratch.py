from pathlib import Path

import pytest

from pico.core.actions import (
    InvalidActionError,
    load_table_tool,
    sql_tool,
)
from pico.core.scratch import MAX_ROWS, Scratch, scratch_path
from pico.core.tools import ToolError
from pico.session import (
    Session,
    ToolCallRecorded,
    connect,
)


def _scratch(tmp_path: Path) -> Scratch:
    return Scratch(Session(connect(tmp_path / "session.db"), "s1"))


def _csv(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def test_load_table_creates_table_with_sanitised_columns_and_row_count(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "Item Name,Unit Price ($),2024\nbolt,2,x\nnut,1,y\n")
    scratch = _scratch(tmp_path)

    result = load_table_tool(scratch).execute({"path": path, "table": "Inventory Feed"})

    assert result == "table inventory_feed(item_name, unit_price, c_2024) loaded with 2 rows"


def test_load_table_result_holds_no_data_rows(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "item,count\nbolt,12\n")
    scratch = _scratch(tmp_path)

    result = load_table_tool(scratch).execute({"path": path, "table": "inventory"})

    assert "bolt" not in result and "12" not in result


def test_load_table_duplicate_header_names_are_made_unique(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "name,name,name\na,b,c\n")
    scratch = _scratch(tmp_path)

    result = load_table_tool(scratch).execute({"path": path, "table": "t"})

    assert "name, name_2, name_3" in result


def test_load_table_stores_numeric_columns_as_numbers_and_others_as_text(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "label,count,ratio\nbolt,12,0.5\nnut,7,1.25\n")
    scratch = _scratch(tmp_path)
    load_table_tool(scratch).execute({"path": path, "table": "t"})

    rows = scratch.connection.execute("SELECT label, count, ratio FROM t ORDER BY count").fetchall()

    assert rows == [("nut", 7, 1.25), ("bolt", 12, 0.5)]


def test_load_table_blank_cells_in_numeric_column_become_null(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "label,count\nbolt,\nnut,7\n")
    scratch = _scratch(tmp_path)
    load_table_tool(scratch).execute({"path": path, "table": "t"})

    assert scratch.connection.execute("SELECT SUM(count) FROM t").fetchone() == (7,)


def test_load_table_replaces_an_existing_table_of_the_same_name(tmp_path: Path) -> None:
    first = _csv(tmp_path, "a.csv", "item,count\nbolt,12\nnut,3\n")
    second = _csv(tmp_path, "b.csv", "item,count\nwasher,9\n")
    scratch = _scratch(tmp_path)
    load_table_tool(scratch).execute({"path": first, "table": "t"})

    result = load_table_tool(scratch).execute({"path": second, "table": "t"})

    assert "loaded with 1 rows" in result
    assert scratch.connection.execute("SELECT item FROM t").fetchall() == [("washer",)]


def test_load_table_missing_file_raises_tool_error(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)

    with pytest.raises(ToolError, match="could not read"):
        load_table_tool(scratch).execute({"path": str(tmp_path / "gone.csv"), "table": "t"})


def test_load_table_empty_file_raises_tool_error(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "")
    scratch = _scratch(tmp_path)

    with pytest.raises(ToolError, match="must be a header"):
        load_table_tool(scratch).execute({"path": path, "table": "t"})


def test_load_table_blank_header_row_raises_tool_error(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", ",,\n1,2,3\n")
    scratch = _scratch(tmp_path)

    with pytest.raises(ToolError, match="empty header row"):
        load_table_tool(scratch).execute({"path": path, "table": "t"})


def test_load_table_missing_argument_raises_invalid_action_error(tmp_path: Path) -> None:
    with pytest.raises(InvalidActionError, match="missing required field 'table'"):
        load_table_tool(_scratch(tmp_path)).execute({"path": "a.csv"})


def test_load_table_row_longer_than_header_raises_tool_error(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "id,name\n1,Smith, John\n")

    with pytest.raises(ToolError, match="line 2 has 3 cells"):
        load_table_tool(_scratch(tmp_path)).execute({"path": path, "table": "t"})


def test_load_table_protected_table_name_raises_tool_error(tmp_path: Path) -> None:
    path = _csv(tmp_path, "a.csv", "a,b\n1,2\n")

    with pytest.raises(ToolError):
        load_table_tool(_scratch(tmp_path)).execute({"path": path, "table": "sqlite_master"})


def test_scratch_path_sits_next_to_the_session_database(tmp_path: Path) -> None:
    session = Session(connect(tmp_path / "session.db"), "sess/child")

    assert scratch_path(session) == str(tmp_path / "scratch-sess-child.db")


def test_scratch_path_is_in_memory_for_an_in_memory_session() -> None:
    assert scratch_path(Session(connect(":memory:"), "s1")) == ":memory:"


def _loaded(tmp_path: Path) -> Scratch:
    scratch = _scratch(tmp_path)
    load_table_tool(scratch).execute(
        {"path": _csv(tmp_path, "i.csv", "item,count\nbolt,12\nnut,30\n"), "table": "inventory"}
    )
    load_table_tool(scratch).execute(
        {"path": _csv(tmp_path, "p.csv", "item,price\nbolt,2\nnut,1\n"), "table": "prices"}
    )
    return scratch


def test_sql_joins_and_aggregates_render_as_an_aligned_table(tmp_path: Path) -> None:
    result = sql_tool(_loaded(tmp_path)).execute(
        {
            "query": "SELECT i.item, i.count * p.price AS value FROM inventory i "
            "JOIN prices p ON p.item = i.item ORDER BY i.item"
        }
    )

    assert result == "item  value\nbolt  24\nnut   30"


def test_sql_aggregate_over_a_join_computes_the_total(tmp_path: Path) -> None:
    result = sql_tool(_loaded(tmp_path)).execute(
        {
            "query": "SELECT SUM(i.count * p.price) AS total FROM inventory i "
            "JOIN prices p ON p.item = i.item"
        }
    )

    assert result == "total\n54"


def test_sql_truncates_beyond_the_row_cap_with_a_more_rows_note(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    rows = "\n".join(f"row-{index},{index}" for index in range(MAX_ROWS + 7))
    load_table_tool(scratch).execute(
        {"path": _csv(tmp_path, "big.csv", f"name,n\n{rows}\n"), "table": "big"}
    )

    result = sql_tool(scratch).execute({"query": "SELECT name FROM big ORDER BY n"})

    assert result.splitlines()[-1] == f"only the first {MAX_ROWS} rows are shown"
    assert len(result.splitlines()) == MAX_ROWS + 2


def test_sql_empty_result_set_says_no_rows(tmp_path: Path) -> None:
    result = sql_tool(_loaded(tmp_path)).execute(
        {"query": "SELECT item FROM inventory WHERE count > 1000"}
    )

    assert result == "no rows"


def test_sql_null_cells_render_as_blanks(tmp_path: Path) -> None:
    result = sql_tool(_loaded(tmp_path)).execute({"query": "SELECT NULL AS a, 1 AS b"})

    assert result == "a  b\n   1"


def test_sql_invalid_statement_raises_tool_error_with_the_sqlite_message(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match='near "SELEKT": syntax error'):
        sql_tool(_loaded(tmp_path)).execute({"query": "SELEKT 1"})


def test_sql_cannot_reach_the_session_event_log(tmp_path: Path) -> None:
    session = Session(connect(tmp_path / "session.db"), "s1")
    session.append(ToolCallRecorded(name="shell", arguments={}, result="x", is_error=False))

    with pytest.raises(ToolError, match="no such table: events"):
        sql_tool(Scratch(session)).execute({"query": "SELECT * FROM events"})


def test_sql_create_and_insert_stage_intermediate_results(tmp_path: Path) -> None:
    scratch = _loaded(tmp_path)

    assert (
        sql_tool(scratch).execute({"query": "CREATE TABLE totals (item TEXT, value NUMERIC)"})
        == "ok"
    )
    sql_tool(scratch).execute(
        {
            "query": "INSERT INTO totals SELECT i.item, i.count * p.price FROM inventory i "
            "JOIN prices p ON p.item = i.item"
        }
    )

    assert sql_tool(scratch).execute({"query": "SELECT SUM(value) AS t FROM totals"}) == "t\n54"


def test_sql_missing_query_argument_raises_invalid_action_error(tmp_path: Path) -> None:
    with pytest.raises(InvalidActionError, match="missing required field 'query'"):
        sql_tool(_scratch(tmp_path)).execute({})


def test_sql_aborts_a_runaway_statement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pico.core.scratch.QUERY_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(ToolError, match="timed out"):
        sql_tool(_scratch(tmp_path)).execute(
            {
                "query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                "SELECT COUNT(*) FROM c"
            }
        )


def test_sql_spec_warns_against_using_it_as_a_shell_command(tmp_path: Path) -> None:
    description = sql_tool(_scratch(tmp_path)).spec.description

    assert "not a shell command" in description
    assert "verify" in description
