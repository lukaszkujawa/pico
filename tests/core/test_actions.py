from pathlib import Path

import pytest

from pico.core.actions import (
    Answer,
    InvalidActionError,
    ReadFile,
    Shell,
    WriteFile,
    register_actions,
)
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolCall


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


def test_read_file_execute_missing_file_returns_error_string(tmp_path: Path) -> None:
    path = tmp_path / "missing.txt"
    action = ReadFile(path=str(path))
    result = action.execute()
    assert "could not read" in result


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


def test_write_file_execute_missing_parent_returns_error_string(tmp_path: Path) -> None:
    path = tmp_path / "missing_dir" / "out.txt"
    action = WriteFile(path=str(path), content="hello")
    result = action.execute()
    assert "could not write" in result


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


def test_shell_execute_timeout_returns_error_string() -> None:
    action = Shell(command="sleep 5")
    result = action.execute(timeout=0.1)
    assert "timed out" in result


def test_answer_from_arguments() -> None:
    action = Answer.from_arguments({"content": "the answer"})
    assert action == Answer(content="the answer")


def test_answer_from_arguments_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        Answer.from_arguments({})


def test_register_actions_populates_all_four_names() -> None:
    registry = ToolRegistry()
    register_actions(registry)

    names = {spec.name for spec in registry.specs()}

    assert names == {"read_file", "write_file", "shell", "answer"}


def test_register_actions_read_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello")
    registry = ToolRegistry()
    register_actions(registry)

    result = registry.execute(ToolCall(id="1", name="read_file", arguments={"path": str(path)}))

    assert result == "hello"


def test_register_actions_write_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    registry = ToolRegistry()
    register_actions(registry)

    result = registry.execute(
        ToolCall(id="1", name="write_file", arguments={"path": str(path), "content": "hi"})
    )

    assert "wrote" in result
    assert path.read_text() == "hi"


def test_register_actions_shell_round_trips() -> None:
    registry = ToolRegistry()
    register_actions(registry)

    result = registry.execute(ToolCall(id="1", name="shell", arguments={"command": "echo hi"}))

    assert result.strip() == "hi"


def test_register_actions_invalid_arguments_return_error_string_not_raise() -> None:
    registry = ToolRegistry()
    register_actions(registry)

    result = registry.execute(ToolCall(id="1", name="read_file", arguments={}))

    assert "missing required field" in result
