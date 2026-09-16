import runpy
import sys

import pytest

import pico
from pico.config import Config, ConfigError


def _config() -> Config:
    return Config(
        vendor="ollama",
        base_url="http://localhost:11434",
        model="qwen3",
        api_key=None,
        context_size=1024,
        session_path="pico.db",
    )


def test_main_calls_run_pico_with_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[tuple[Config, bool]] = []

    def fake_run_pico(cfg: Config, debug: bool = False) -> None:
        received.append((cfg, debug))

    monkeypatch.setattr(pico, "load_config", lambda: config)
    monkeypatch.setattr(pico, "run_pico", fake_run_pico)
    monkeypatch.setattr(sys, "argv", ["pico"])

    pico.main()

    assert received == [(config, False)]


def test_main_passes_debug_flag_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    received: list[tuple[Config, bool]] = []

    def fake_run_pico(cfg: Config, debug: bool = False) -> None:
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
