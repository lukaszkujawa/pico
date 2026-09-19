from pathlib import Path

import pytest

from pico.core.actions import (
    InvalidActionError,
    edit_file_tool,
    read_file_tool,
    write_file_tool,
)
from pico.core.actions.files import READ_FILE_CAP_CHARS
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


def test_read_file_slice_returns_header_and_requested_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\nthree\nfour\n")

    result = read_file_tool().execute({"path": str(path), "offset": 2, "limit": 2})

    assert result == "lines 2-3 of 4:\ntwo\nthree\n"


def test_read_file_slice_defaults_offset_to_first_line(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\nthree\n")

    result = read_file_tool().execute({"path": str(path), "limit": 1})

    assert result == "lines 1-1 of 3:\none\n"


def test_read_file_slice_clamps_end_to_file_length(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo")

    result = read_file_tool().execute({"path": str(path), "offset": 2, "limit": 10})

    assert result == "lines 2-2 of 2:\ntwo"


def test_read_file_slice_out_of_range_offset_names_line_count(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\n")

    with pytest.raises(ToolError, match=r"offset 5 is out of range: .* has 2 lines"):
        read_file_tool().execute({"path": str(path), "offset": 5})


def test_read_file_slice_rejects_nonpositive_limit(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("one\n")

    with pytest.raises(ToolError, match="limit must be at least 1"):
        read_file_tool().execute({"path": str(path), "offset": 1, "limit": 0})


def test_read_file_under_cap_is_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    content = "x" * READ_FILE_CAP_CHARS
    path.write_text(content)

    assert read_file_tool().execute({"path": str(path)}) == content


def test_read_file_over_cap_ends_with_totals_and_hint(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    content = "".join(f"line {n}\n" for n in range(1000))
    path.write_text(content)

    result = read_file_tool().execute({"path": str(path)})

    assert result.startswith("line 0\nline 1\n")
    assert result.endswith(
        f"… {len(content)} chars / 1000 lines total — pass offset and limit to read more"
    )
    head = result[: result.index("…")]
    assert len(head) <= READ_FILE_CAP_CHARS
    assert content.startswith(head)


def test_read_file_successive_slices_reconstruct_exact_content(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    content = "".join(f"line {n}\n" for n in range(1000)) + "tail without newline"
    path.write_text(content)

    pieces: list[str] = []
    offset = 1
    while offset <= 1001:
        result = read_file_tool().execute({"path": str(path), "offset": offset, "limit": 100})
        pieces.append(result.split("\n", 1)[1])
        offset += 100

    assert "".join(pieces) == content


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


def test_edit_file_missing_field() -> None:
    with pytest.raises(InvalidActionError):
        edit_file_tool().execute({"path": "a.txt", "old_text": "x"})


def test_edit_file_replaces_unique_occurrence(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("alpha beta gamma")

    result = edit_file_tool().execute({"path": str(path), "old_text": "beta", "new_text": "delta"})

    assert path.read_text() == "alpha delta gamma"
    assert result == f"edited {path}: replaced 4 chars with 5 chars"
    assert "delta" not in result.replace(str(path), "")


def test_edit_file_absent_text_leaves_file_unmodified(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("alpha beta")

    with pytest.raises(ToolError, match=f"old_text not found in {path}"):
        edit_file_tool().execute({"path": str(path), "old_text": "zeta", "new_text": "eta"})

    assert path.read_text() == "alpha beta"


def test_edit_file_ambiguous_text_reports_count_and_leaves_file_unmodified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.txt"
    path.write_text("spam spam eggs spam")

    with pytest.raises(ToolError, match=r"occurs 3 times.*surrounding context"):
        edit_file_tool().execute({"path": str(path), "old_text": "spam", "new_text": "ham"})

    assert path.read_text() == "spam spam eggs spam"


def test_edit_file_identical_texts_rejected(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("alpha")

    with pytest.raises(ToolError, match="identical"):
        edit_file_tool().execute({"path": str(path), "old_text": "alpha", "new_text": "alpha"})

    assert path.read_text() == "alpha"


def test_edit_file_unreadable_path_raises_tool_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.txt"
    with pytest.raises(ToolError, match="could not read"):
        edit_file_tool().execute({"path": str(path), "old_text": "a", "new_text": "b"})
