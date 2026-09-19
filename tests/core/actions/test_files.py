from pathlib import Path

import pytest

from pico.core.actions import (
    InvalidActionError,
    read_file_tool,
    write_file_tool,
)
from pico.core.tools import ToolError


def test_read_file_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        read_file_tool().execute({})


def test_read_file_wrong_type() -> None:
    with pytest.raises(InvalidActionError):
        read_file_tool().execute({"path": 1})


def test_read_file_reads_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello")
    assert read_file_tool().execute({"path": str(path)}) == "hello"


def test_read_file_missing_file_raises_tool_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.txt"
    with pytest.raises(ToolError, match="could not read"):
        read_file_tool().execute({"path": str(path)})


def test_write_file_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        write_file_tool().execute({"path": "a.txt"})


def test_write_file_wrong_type() -> None:
    with pytest.raises(InvalidActionError):
        write_file_tool().execute({"path": "a.txt", "content": 1})


def test_write_file_writes_file(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    result = write_file_tool().execute({"path": str(path), "content": "hello world"})
    assert "wrote" in result
    assert path.read_text() == "hello world"


def test_write_file_missing_parent_raises_tool_error(tmp_path: Path) -> None:
    path = tmp_path / "missing_dir" / "out.txt"
    with pytest.raises(ToolError, match="could not write"):
        write_file_tool().execute({"path": str(path), "content": "hello"})
