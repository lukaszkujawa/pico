from collections.abc import Iterator

import pytest

from pico.config import Config, ConfigError, load_config

REQUIRED_VARS = {
    "LLM_VENDOR": "ollama",
    "LLM_BASE_URL": "http://localhost:11434/v1",
    "LLM_MODEL": "qwen3.8:latest",
    "LLM_API_KEY": "secret",
    "LLM_CONTEXT_SIZE": "128000",
    "SESSION_DB_PATH": "pico.db",
}

ENV_KEYS = [
    "LLM_TEMPERATURE",
    "LLM_VISION",
    "LLM_VENDOR",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_CONTEXT_SIZE",
    "SESSION_DB_PATH",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir("/tmp")
    yield


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {**REQUIRED_VARS, **overrides}
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_load_config_returns_populated_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    config = load_config()

    assert config == Config(
        vendor="ollama",
        base_url="http://localhost:11434/v1",
        model="qwen3.8:latest",
        api_key="secret",
        context_size=128000,
        session_path="pico.db",
    )


@pytest.mark.parametrize(
    "missing", ["LLM_BASE_URL", "LLM_MODEL", "LLM_CONTEXT_SIZE", "SESSION_DB_PATH"]
)
def test_missing_required_variable_raises_config_error(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ConfigError, match=missing):
        load_config()


def test_empty_api_key_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LLM_API_KEY="")

    config = load_config()

    assert config.api_key is None


def test_non_integer_context_size_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LLM_CONTEXT_SIZE="not-a-number")

    with pytest.raises(ConfigError, match="LLM_CONTEXT_SIZE"):
        load_config()


def test_temperature_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    assert load_config().temperature is None


def test_temperature_is_parsed_as_float(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LLM_TEMPERATURE="0.2")

    assert load_config().temperature == 0.2


def test_non_numeric_temperature_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, LLM_TEMPERATURE="warm")

    with pytest.raises(ConfigError, match="LLM_TEMPERATURE"):
        load_config()


def test_vision_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    assert load_config().vision is False


@pytest.mark.parametrize("value", ["1", "true", "True"])
def test_vision_is_enabled_by_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _set_env(monkeypatch, LLM_VISION=value)

    assert load_config().vision is True


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_vision_stays_off_for_other_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _set_env(monkeypatch, LLM_VISION=value)

    assert load_config().vision is False
