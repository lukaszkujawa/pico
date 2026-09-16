import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    vendor: str
    base_url: str
    model: str
    api_key: str | None
    context_size: int
    session_path: str


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def load_config() -> Config:
    load_dotenv(find_dotenv(usecwd=True))

    vendor = _require("LLM_VENDOR")
    base_url = _require("LLM_BASE_URL")
    model = _require("LLM_MODEL")

    api_key = os.environ.get("LLM_API_KEY") or None

    context_size_raw = _require("LLM_CONTEXT_SIZE")
    try:
        context_size = int(context_size_raw)
    except ValueError as error:
        raise ConfigError(
            f"LLM_CONTEXT_SIZE must be an integer, got: {context_size_raw!r}"
        ) from error

    session_path = _require("SESSION_DB_PATH")

    return Config(
        vendor=vendor,
        base_url=base_url,
        model=model,
        api_key=api_key,
        context_size=context_size,
        session_path=session_path,
    )
