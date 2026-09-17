import uuid
from pathlib import Path

import pytest

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    Answer,
    CompleteStep,
    Delegate,
    InvalidActionError,
    ReadFile,
    ResultShape,
    SetPlan,
    Shell,
    WriteFile,
    complete_step_tool,
    fact_recall_tool,
    load_table_tool,
    register_actions,
    set_plan_tool,
    sql_tool,
)
from pico.core.errors import ToolError
from pico.core.scratch import MAX_ROWS, Scratch, scratch_path
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolCall
from pico.session import PlanSet, PlanStepCompleted, Session, ToolCallRecorded, connect
from tests.conftest import wait_until


def _session_with_fact(content: str) -> tuple[Session, int]:
    session = Session(connect(":memory:"), "s1")
    session.append(ToolCallRecorded(name="shell", arguments={}, result=content, is_error=False))
    return session, 1


def test_read_file_from_arguments() -> None:
    action = ReadFile.from_arguments({"path": "a.txt"})
    assert action == ReadFile(path="a.txt")


def test_read_file_from_arguments_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        ReadFile.from_arguments({})


def test_read_file_from_arguments_wrong_type() -> None:
    with pytest.raises(InvalidActionError):
        ReadFile.from_arguments({"path": 1})


def test_read_file_execute_reads_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello")
    action = ReadFile(path=str(path))
    assert action.execute() == "hello"


def test_read_file_execute_missing_file_raises_tool_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.txt"
    action = ReadFile(path=str(path))
    with pytest.raises(ToolError, match="could not read"):
        action.execute()


def test_write_file_from_arguments() -> None:
    action = WriteFile.from_arguments({"path": "a.txt", "content": "hi"})
    assert action == WriteFile(path="a.txt", content="hi")


def test_write_file_from_arguments_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        WriteFile.from_arguments({"path": "a.txt"})


def test_write_file_from_arguments_wrong_type() -> None:
    with pytest.raises(InvalidActionError):
        WriteFile.from_arguments({"path": "a.txt", "content": 1})


def test_write_file_execute_writes_file(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    action = WriteFile(path=str(path), content="hello world")
    result = action.execute()
    assert "wrote" in result
    assert path.read_text() == "hello world"


def test_write_file_execute_missing_parent_raises_tool_error(tmp_path: Path) -> None:
    path = tmp_path / "missing_dir" / "out.txt"
    action = WriteFile(path=str(path), content="hello")
    with pytest.raises(ToolError, match="could not write"):
        action.execute()


def test_shell_from_arguments() -> None:
    action = Shell.from_arguments({"command": "echo hi"})
    assert action == Shell(command="echo hi")


def test_shell_from_arguments_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        Shell.from_arguments({})


def test_shell_from_arguments_wrong_type() -> None:
    with pytest.raises(InvalidActionError):
        Shell.from_arguments({"command": 1})


def test_shell_execute_returns_stdout() -> None:
    action = Shell(command="echo hello")
    assert action.execute().strip() == "hello"


def test_shell_execute_failing_command_includes_exit_code() -> None:
    action = Shell(command="exit 3")
    result = action.execute()
    assert "exit code 3" in result


def test_shell_execute_timeout_raises_tool_error() -> None:
    action = Shell(command="sleep 5")
    with pytest.raises(ToolError, match="timed out"):
        action.execute(timeout=0.1)


def test_register_actions_read_file_missing_path_raises_tool_error(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    with pytest.raises(ToolError):
        registry.execute(
            ToolCall(id="1", name="read_file", arguments={"path": str(tmp_path / "nope.txt")})
        )


def test_answer_from_arguments() -> None:
    action = Answer.from_arguments({"content": "the answer", "citations": [0, 1]})
    assert action == Answer(content="the answer", citations=(0, 1))


def test_answer_from_arguments_missing_content() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"citations": []})


def test_answer_from_arguments_missing_citations() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer"})


def test_answer_from_arguments_citations_not_a_list() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer", "citations": "0"})


def test_answer_from_arguments_citations_with_non_int_element() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer", "citations": [0, "1"]})


def test_answer_from_arguments_without_verify_is_none() -> None:
    action = Answer.from_arguments({"content": "the answer", "citations": []})
    assert action.verify is None


def test_answer_from_arguments_with_verify_round_trips() -> None:
    action = Answer.from_arguments(
        {"content": "the answer", "citations": [], "verify": "test -f out.txt"}
    )
    assert action == Answer(content="the answer", citations=(), verify="test -f out.txt")


def test_answer_from_arguments_verify_not_a_string() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({"content": "the answer", "citations": [], "verify": 1})


def test_answer_from_arguments_verify_empty_string() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        Answer.from_arguments({"content": "the answer", "citations": [], "verify": "   "})


def test_answer_tool_spec_documents_verify() -> None:
    registry = ToolRegistry()
    register_actions(registry, _session_with_fact("x")[0])
    spec = next(spec for spec in registry.specs() if spec.name == "answer")
    properties = spec.parameters["properties"]
    required = spec.parameters["required"]
    assert isinstance(properties, dict)
    assert isinstance(required, list)
    assert "verify" in properties
    assert "verify" not in required
    assert "exits 0" in spec.description


def test_shell_run_returns_exit_code_and_output() -> None:
    assert Shell(command="echo hi").run() == (0, "hi\n")
    code, output = Shell(command="echo boom >&2; exit 3").run()
    assert code == 3
    assert "boom" in output


def test_shell_run_invokes_callback_per_chunk_and_preserves_combined_output() -> None:
    chunks: list[str] = []
    code, output = Shell(command="echo one; sleep 0.2; echo two").run(on_chunk=chunks.append)

    assert code == 0
    assert len(chunks) > 1
    assert "".join(chunks) == output
    assert output == "one\ntwo\n"


def test_shell_run_times_out_on_output_without_a_trailing_newline() -> None:
    with pytest.raises(ToolError, match="timed out"):
        Shell(command="printf partial; sleep 5").run(timeout=0.2)


def test_shell_run_reports_non_zero_exit_code_with_callback() -> None:
    chunks: list[str] = []
    code, output = Shell(command="echo boom >&2; exit 3").run(on_chunk=chunks.append)

    assert code == 3
    assert "boom" in output
    assert "".join(chunks) == output


def test_shell_run_timeout_raises_tool_error_and_kills_process() -> None:
    marker = f"pico-timeout-{uuid.uuid4().hex}"

    with pytest.raises(ToolError, match="timed out"):
        Shell(command=f"sleep 5 # {marker}").run(timeout=0.1)

    def killed() -> bool:
        _, output = Shell(command=f"pgrep -f {marker} >/dev/null; echo $?").run()
        return output.strip() == "1"

    wait_until(killed, "the timed-out process is gone")


def test_delegate_from_arguments() -> None:
    action = Delegate.from_arguments({"question": "what is x?"})
    assert action == Delegate(question="what is x?")


def test_delegate_from_arguments_missing_question() -> None:
    with pytest.raises(InvalidActionError):
        Delegate.from_arguments({})


def test_delegate_from_arguments_with_fields() -> None:
    action = Delegate.from_arguments(
        {"question": "how many?", "fields": {"count": "number", "name": "string"}}
    )

    assert action == Delegate(
        question="how many?", shape=ResultShape({"count": "number", "name": "string"})
    )


def test_delegate_from_arguments_with_null_fields_is_untyped() -> None:
    assert Delegate.from_arguments({"question": "q", "fields": None}).shape is None


def test_delegate_from_arguments_rejects_unknown_field_type() -> None:
    with pytest.raises(InvalidActionError, match="unknown type 'date'"):
        Delegate.from_arguments({"question": "q", "fields": {"when": "date"}})


def test_delegate_from_arguments_rejects_non_object_fields() -> None:
    with pytest.raises(InvalidActionError, match="must be an object"):
        Delegate.from_arguments({"question": "q", "fields": ["count"]})


def test_delegate_from_arguments_rejects_empty_fields() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        Delegate.from_arguments({"question": "q", "fields": {}})


def test_result_shape_prompt_names_every_field() -> None:
    shape = ResultShape({"count": "number", "ok": "boolean"})

    assert '"count": <number>' in shape.prompt()
    assert '"ok": <boolean>' in shape.prompt()


def test_result_shape_check_accepts_conforming_record() -> None:
    shape = ResultShape({"count": "number", "ok": "boolean", "n": "string"})

    assert shape.check('{"count": 3, "ok": true, "n": "a"}') is None


def test_result_shape_check_rejects_boolean_where_number_expected() -> None:
    shape = ResultShape({"count": "number"})

    problem = shape.check('{"count": true}')

    assert problem is not None
    assert "must be a number" in problem


def test_result_shape_check_rejects_non_object_json() -> None:
    shape = ResultShape({"count": "number"})

    problem = shape.check("[1, 2]")

    assert problem is not None
    assert "must be a JSON object" in problem


def test_read_fact_returns_original_content() -> None:
    session, fact_id = _session_with_fact("x" * 5000)

    result = fact_recall_tool(session).execute({"id": fact_id})

    assert result == "x" * 5000


def test_read_fact_unknown_id_raises_tool_error_naming_the_id() -> None:
    session, _ = _session_with_fact("hello")

    with pytest.raises(ToolError, match="99"):
        fact_recall_tool(session).execute({"id": 99})


def test_read_fact_error_result_is_not_a_fact() -> None:
    session = Session(connect(":memory:"), "s1")
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))

    with pytest.raises(ToolError):
        fact_recall_tool(session).execute({"id": 1})


def test_read_fact_non_integer_id_raises_invalid_action_error() -> None:
    session, _ = _session_with_fact("hello")

    with pytest.raises(InvalidActionError, match="must be a int"):
        fact_recall_tool(session).execute({"id": "1"})


def test_read_fact_missing_id_raises_invalid_action_error() -> None:
    session, _ = _session_with_fact("hello")

    with pytest.raises(InvalidActionError, match="missing required field"):
        fact_recall_tool(session).execute({})


def test_register_actions_populates_all_tool_names() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    names = {spec.name for spec in registry.specs()}

    assert names == {
        "read_file",
        "write_file",
        "shell",
        "load_table",
        "sql",
        "read_fact",
        "set_plan",
        "complete_step",
        "answer",
        "delegate",
    }


def test_register_actions_below_max_depth_still_has_full_names() -> None:
    registry = ToolRegistry()
    register_actions(
        registry, Session(connect(":memory:"), "s1/delegate/1"), depth=MAX_DELEGATE_DEPTH - 1
    )

    names = {spec.name for spec in registry.specs()}

    assert "shell" in names
    assert "set_plan" in names
    assert "delegate" in names


def test_register_actions_withholds_delegate_at_max_depth() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"), depth=MAX_DELEGATE_DEPTH)

    names = {spec.name for spec in registry.specs()}

    assert "delegate" not in names
    assert "shell" in names


def test_register_actions_read_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello")
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    result = registry.execute(ToolCall(id="1", name="read_file", arguments={"path": str(path)}))

    assert result == "hello"


def test_register_actions_write_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    result = registry.execute(
        ToolCall(id="1", name="write_file", arguments={"path": str(path), "content": "hi"})
    )

    assert "wrote" in result
    assert path.read_text() == "hi"


def test_register_actions_shell_round_trips() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    result = registry.execute(ToolCall(id="1", name="shell", arguments={"command": "echo hi"}))

    assert result.strip() == "hi"


def test_register_actions_invalid_arguments_raise_invalid_action_error() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    with pytest.raises(InvalidActionError, match="missing required field"):
        registry.execute(ToolCall(id="1", name="read_file", arguments={}))


def _planless_session() -> Session:
    return Session(connect(":memory:"), "s1")


def test_set_plan_from_arguments() -> None:
    assert SetPlan.from_arguments({"steps": ["one", "two"]}) == SetPlan(steps=("one", "two"))


def test_set_plan_from_arguments_empty_list_is_invalid() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        SetPlan.from_arguments({"steps": []})


def test_set_plan_from_arguments_non_string_element_is_invalid() -> None:
    with pytest.raises(InvalidActionError, match="list of strings"):
        SetPlan.from_arguments({"steps": ["one", 2]})


def test_complete_step_from_arguments() -> None:
    assert CompleteStep.from_arguments({"index": 3}) == CompleteStep(index=3)


def test_set_plan_appends_event_and_returns_checklist() -> None:
    session = _planless_session()

    result = set_plan_tool(session).execute({"steps": ["read the file", "write the answer"]})

    assert list(session.events()) == [PlanSet(steps=("read the file", "write the answer"))]
    assert result == "plan set:\n[ ] 0. read the file\n[ ] 1. write the answer"


def test_set_plan_with_empty_steps_raises_and_appends_nothing() -> None:
    session = _planless_session()

    with pytest.raises(InvalidActionError, match="must not be empty"):
        set_plan_tool(session).execute({"steps": []})

    assert list(session.events()) == []


def test_set_plan_with_malformed_steps_raises_and_appends_nothing() -> None:
    session = _planless_session()

    with pytest.raises(InvalidActionError, match="must be a list"):
        set_plan_tool(session).execute({"steps": "one"})

    assert list(session.events()) == []


def test_complete_step_checks_the_box() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one", "two"]})

    result = complete_step_tool(session).execute({"index": 0})

    assert list(session.events())[-1] == PlanStepCompleted(index=0)
    assert result == "step 0 done:\n[x] 0. one\n[ ] 1. two"


def test_complete_step_without_a_plan_raises_tool_error() -> None:
    session = _planless_session()

    with pytest.raises(ToolError, match="no plan set"):
        complete_step_tool(session).execute({"index": 0})

    assert list(session.events()) == []


def test_complete_step_out_of_range_raises_tool_error_and_appends_nothing() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one"]})

    with pytest.raises(ToolError, match="no plan step at index 5"):
        complete_step_tool(session).execute({"index": 5})

    assert list(session.events()) == [PlanSet(steps=("one",))]


def test_complete_step_already_done_raises_tool_error_and_appends_nothing() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one"]})
    complete_step_tool(session).execute({"index": 0})
    before = list(session.events())

    with pytest.raises(ToolError, match="already done"):
        complete_step_tool(session).execute({"index": 0})

    assert list(session.events()) == before


def test_complete_step_non_integer_index_raises_invalid_action_error() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one"]})

    with pytest.raises(InvalidActionError, match="must be a int"):
        complete_step_tool(session).execute({"index": "0"})


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
