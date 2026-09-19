import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from pico import cli
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

    def fake_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        received.append((cfg, debug))

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico"])

    cli.main()

    assert received == [(config, False)]


def test_main_passes_debug_flag_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[tuple[Config, bool]] = []

    def fake_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        received.append((cfg, debug))

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico", "--debug"])

    cli.main()

    assert received == [(config, True)]


def test_main_passes_prompt_to_run_pico(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[str | None] = []

    def fake_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        received.append(initial_prompt)

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico", "--prompt", "do x and y"])

    cli.main()

    assert received == ["do x and y"]


def test_main_defaults_prompt_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[str | None] = []

    def fake_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        received.append(initial_prompt)

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico"])

    cli.main()

    assert received == [None]


def test_main_passes_mailbox_to_run_pico(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[str | None] = []

    def fake_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        received.append(mailbox)

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico", "--mailbox", "./sock/worker"])

    cli.main()

    assert received == ["./sock/worker"]


def test_main_reports_config_error_from_run_pico_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config()

    def failing_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        raise ConfigError("cannot create mailbox file at ./sock/worker: Permission denied")

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", failing_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico", "--mailbox", "./sock/worker"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "cannot create mailbox file" in capsys.readouterr().err


def test_main_exits_cleanly_on_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> Config:
        raise ConfigError("missing required environment variable: LLM_MODEL")

    monkeypatch.setattr(cli, "load_config", fail)
    monkeypatch.setattr(sys, "argv", ["pico"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code != 0


def test_dunder_main_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    monkeypatch.setattr(cli, "main", lambda: called.append(True))

    runpy.run_module("pico.__main__", run_name="__main__")

    assert called == [True]


def _record_session_ids(monkeypatch: pytest.MonkeyPatch, config: Config) -> list[str | None]:
    received: list[str | None] = []

    def fake_run_pico(
        cfg: Config,
        debug: bool = False,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        mailbox: str | None = None,
    ) -> None:
        received.append(session_id)

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "run_pico", fake_run_pico)
    return received


def test_no_resume_flag_generates_a_fresh_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    conn = connect(config.session_path)
    Session(conn, "earlier").append(UserMessageRecorded(content="hello"))

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico"])

    cli.main()

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

    cli.main()

    assert received == ["newer"]


def test_resume_with_id_resolves_to_that_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    connect(config.session_path)

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico", "--resume", "abc123"])

    cli.main()

    assert received == ["abc123"]


def test_bare_resume_on_empty_database_falls_back_to_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(str(tmp_path / "session.db"))
    connect(config.session_path)

    received = _record_session_ids(monkeypatch, config)
    monkeypatch.setattr(sys, "argv", ["pico", "--resume"])

    cli.main()

    assert received == [None]


def test_import_pico_pulls_in_no_application_modules() -> None:
    probe = (
        "import sys\n"
        "import pico\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name == 'pico.app' or name.split('.')[0] in {'textual', 'httpx'}\n"
        ")\n"
        "assert not leaked, leaked\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
