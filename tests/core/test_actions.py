from pathlib import Path

import pytest

from pico.core.actions import (
    Answer,
    Delegate,
    InvalidActionError,
    ReadFile,
    Shell,
    WriteFile,
    fact_recall_tool,
    register_actions,
    register_delegate_actions,
)
from pico.core.errors import ToolError
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolCall
from pico.session import Session, ToolCallRecorded, connect


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


def test_delegate_from_arguments() -> None:
    action = Delegate.from_arguments({"question": "what is x?"})
    assert action == Delegate(question="what is x?")


def test_delegate_from_arguments_missing_question() -> None:
    with pytest.raises(InvalidActionError):
        Delegate.from_arguments({})


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


def test_read_fact_non_integer_id_returns_invalid_field_error() -> None:
    session, _ = _session_with_fact("hello")

    result = fact_recall_tool(session).execute({"id": "1"})

    assert "must be a int" in result


def test_read_fact_missing_id_returns_invalid_field_error() -> None:
    session, _ = _session_with_fact("hello")

    result = fact_recall_tool(session).execute({})

    assert "missing required field" in result


def test_register_actions_populates_all_six_names() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    names = {spec.name for spec in registry.specs()}

    assert names == {"read_file", "write_file", "shell", "read_fact", "answer", "delegate"}


def test_register_delegate_actions_populates_read_only_names() -> None:
    registry = ToolRegistry()
    register_delegate_actions(registry, Session(connect(":memory:"), "s1"))

    names = {spec.name for spec in registry.specs()}

    assert names == {"read_file", "read_fact", "answer"}


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


def test_register_actions_invalid_arguments_return_error_string_not_raise() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    result = registry.execute(ToolCall(id="1", name="read_file", arguments={}))

    assert "missing required field" in result
