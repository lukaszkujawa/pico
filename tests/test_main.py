import runpy
import sys
from pathlib import Path

import pytest

import pico
from pico.config import Config, ConfigError
from pico.session import Session, UserMessageRecorded, connect


def _config(session_path: str = "pico.db") -> Config:
    return Config(
        vendor="ollama",
        base_url="http://localhost:11434",
        model="qwen3",
        api_key=None,
        context_size=1024,
        session_path=session_path,
    )


def test_main_calls_run_pico_with_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[tuple[Config, bool]] = []

    def fake_run_pico(cfg: Config, debug: bool = False, session_id: str | None = None) -> None:
        received.append((cfg, debug))

    monkeypatch.setattr(pico, "load_config", lambda: config)
    monkeypatch.setattr(pico, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico"])

    pico.main()

    assert received == [(config, False)]


def test_main_passes_debug_flag_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[tuple[Config, bool]] = []

    def fake_run_pico(cfg: Config, debug: bool = False, session_id: str | None = None) -> None:
        received.append((cfg, debug))

    monkeypatch.setattr(pico, "load_config", lambda: config)
    monkeypatch.setattr(pico, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico", "--debug"])

    pico.main()

    assert received == [(config, True)]


def test_main_exits_cleanly_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Config:
        raise ConfigError("missing required environment variable: LLM_MODEL")

    monkeypatch.setattr(pico, "load_config", fail)
    monkeypatch.setattr(sys, "argv", ["pico"])

    with pytest.raises(SystemExit) as excinfo:
        pico.main()

    assert excinfo.value.code != 0


def test_dunder_main_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    monkeypatch.setattr(pico, "main", lambda: called.append(True))

    runpy.run_module("pico.__main__", run_name="__main__")

    assert called == [True]


def _record_session_ids(monkeypatch: pytest.MonkeyPatch, config: Config) -> list[str | None]:
    received: list[str | None] = []

    def fake_run_pico(cfg: Config, debug: bool = False, session_id: str | None = None) -> None:
        received.append(session_id)

    monkeypatch.setattr(pico, "load_config", lambda: config)
    monkeypatch.setattr(pico, "run_pico", fake_run_pico)
    return received


def test_no_resume_flag_generates_a_fresh_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    conn = connect(config.session_path)
    Session(conn, "earlier").append(UserMessageRecorded(content="hello"))

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico"])

    pico.main()

    assert received == [None]


def test_bare_resume_resolves_to_most_recently_active_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    conn = connect(config.session_path)
    Session(conn, "older").append(UserMessageRecorded(content="hello"))
    Session(conn, "newer").append(UserMessageRecorded(content="world"))

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico", "--resume"])

    pico.main()

    assert received == ["newer"]


def test_resume_with_id_resolves_to_that_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    connect(config.session_path)

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico", "--resume", "abc123"])

    pico.main()

    assert received == ["abc123"]


def test_bare_resume_on_empty_database_falls_back_to_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    connect(config.session_path)

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico", "--resume"])

    pico.main()

    assert received == [None]
