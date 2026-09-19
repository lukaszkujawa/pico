from pathlib import Path

import pytest

from pico.core.actions import (
    InvalidActionError,
    ReadFile,
    WriteFile,
)
from pico.core.tools import ToolError


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
