from datetime import datetime
from pathlib import Path

import pytest

from pico.debug.log import RunLog
from pico.llm.types import Message, Role


def test_create_makes_logs_dir_if_absent(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    assert not root.exists()

    RunLog.create(root)

    assert root.is_dir()


def test_create_run_directory_name_matches_timestamp_format(tmp_path: Path) -> None:
    root = tmp_path / "logs"

    run_log = RunLog.create(root)

    name = run_log.directory.name
    assert len(name) == len("2026-09-16_12-00-00")
    assert name[4] == "-" and name[7] == "-" and name[10] == "_"


def test_two_creates_in_same_second_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "logs"

    import pico.debug.log as log_module

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> "FrozenDatetime":
            return cls(2026, 9, 16, 12, 0, 0)

    monkeypatch.setattr(log_module, "datetime", FrozenDatetime)

    first = RunLog.create(root)
    second = RunLog.create(root)

    assert first.directory != second.directory
    assert first.directory.exists()
    assert second.directory.exists()


def test_write_prompt_and_response_share_counter(tmp_path: Path) -> None:
    run_log = RunLog.create(tmp_path / "logs")
    messages = [Message(role=Role.USER, content="hello")]

    run_log.write_prompt(messages)
    run_log.write_response("hi there")
    run_log.write_prompt(messages)
    run_log.write_response("hi again")

    prompt_1 = (run_log.directory / "prompt-1.txt").read_text().splitlines()
    resp_1 = (run_log.directory / "resp-1.txt").read_text().splitlines()
    prompt_2 = (run_log.directory / "prompt-2.txt").read_text().splitlines()
    resp_2 = (run_log.directory / "resp-2.txt").read_text().splitlines()

    assert "hello" in "\n".join(prompt_1[1:])
    assert resp_1[1:] == ["hi there"]
    assert "hello" in "\n".join(prompt_2[1:])
    assert resp_2[1:] == ["hi again"]


def test_write_prompt_first_line_is_timestamp(tmp_path: Path) -> None:
    run_log = RunLog.create(tmp_path / "logs")

    run_log.write_prompt([Message(role=Role.USER, content="hello")])

    lines = (run_log.directory / "prompt-1.txt").read_text().splitlines()
    assert "T" in lines[0]


def test_log_appends_without_truncating(tmp_path: Path) -> None:
    run_log = RunLog.create(tmp_path / "logs")

    run_log.log("first line")
    run_log.log("second line")

    lines = (run_log.directory / "session.log").read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("first line")
    assert lines[1].endswith("second line")
