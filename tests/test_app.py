import threading
import time
from collections.abc import Iterator

import pytest

import pico.app as app_module
from pico.app import UnsupportedVendorError, run_pico
from pico.config import Config
from pico.llm.types import GenerationComplete, Message, StreamEvent, TextDelta, ToolSpec
from pico.tui.app import PicoApp


class SlowClient:
    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield TextDelta(text="hi")
        self._release.wait(timeout=5)
        yield GenerationComplete(finish_reason="stop")


def _patch_ollama_client(monkeypatch: pytest.MonkeyPatch, release: threading.Event) -> None:
    def factory(*, model: str, base_url: str, api_key: str | None) -> SlowClient:
        return SlowClient(release)

    monkeypatch.setattr(app_module, "OllamaClient", factory)


def _config(vendor: str = "ollama") -> Config:
    return Config(
        vendor=vendor,
        base_url="http://localhost:11434",
        model="qwen3",
        api_key=None,
        context_size=1024,
    )


def test_unsupported_vendor_raises_before_starting_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[bool] = []

    def tracking_start(self: threading.Thread) -> None:
        started.append(True)

    monkeypatch.setattr(threading.Thread, "start", tracking_start)

    with pytest.raises(UnsupportedVendorError):
        run_pico(_config(vendor="openai"))

    assert started == []


def test_stopping_tui_does_not_leave_core_thread_running(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    _patch_ollama_client(monkeypatch, release)

    core_threads: list[threading.Thread] = []
    original_init = threading.Thread.__init__

    def tracking_init(self: threading.Thread, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)  # type: ignore[arg-type]
        core_threads.append(self)

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(threading.Thread, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", noop_run)

    run_pico(_config())

    release.set()
    core_threads[0].join(timeout=5)
    assert not core_threads[0].is_alive()


def test_run_pico_waits_for_core_thread_briefly_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(PicoApp, "run", noop_run)

    start = time.monotonic()
    run_pico(_config())
    elapsed = time.monotonic() - start

    assert elapsed < 2
