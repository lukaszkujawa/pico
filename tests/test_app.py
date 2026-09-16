import queue
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import pico.app as app_module
from pico.app import DEFAULT_SESSION_ID, UnsupportedVendorError, run_pico
from pico.config import Config
from pico.core.bus import Bus
from pico.core.events import RunCancelled, RunFinished, RunStarted
from pico.llm.types import GenerationComplete, Message, Role, StreamEvent, TextDelta, ToolSpec
from pico.session import AssistantMessageRecorded, Session, UserMessageRecorded, connect
from pico.tui import PicoApp


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


def _config(tmp_path: Path, vendor: str = "ollama") -> Config:
    return Config(
        vendor=vendor,
        base_url="http://localhost:11434",
        model="qwen3",
        api_key=None,
        context_size=1024,
        session_path=str(tmp_path / "session.db"),
    )


def test_unsupported_vendor_raises_before_starting_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[bool] = []

    def tracking_start(self: threading.Thread) -> None:
        started.append(True)

    monkeypatch.setattr(threading.Thread, "start", tracking_start)

    with pytest.raises(UnsupportedVendorError):
        run_pico(_config(tmp_path, vendor="openai"))

    assert started == []


def test_stopping_tui_does_not_leave_core_thread_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

    run_pico(_config(tmp_path))

    release.set()
    core_threads[0].join(timeout=5)
    assert not core_threads[0].is_alive()


class RecordingClient:
    def __init__(self) -> None:
        self.seen_messages: list[list[Message]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(list(messages))
        yield TextDelta(text="hi")
        yield GenerationComplete(finish_reason="stop")


def test_turn_loop_runs_one_turn_per_queued_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = RecordingClient()

    def factory(*, model: str, base_url: str, api_key: str | None) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        deadline = time.monotonic() + 5
        while len(client.seen_messages) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        input_queue.put("world")
        while len(client.seen_messages) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
    ) -> None:
        queues.append(input_queue)
        original_init(self, bus, input_queue, cancel_handle)

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path))

    assert len(client.seen_messages) == 2
    assert [m.content for m in client.seen_messages[0]] == ["hello"]
    assert [m.content for m in client.seen_messages[1]] == ["hello", "hi", "world"]
    assert [m.role for m in client.seen_messages[1]] == [Role.USER, Role.ASSISTANT, Role.USER]


def test_cancelling_mid_turn_stops_run_and_allows_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    _patch_ollama_client(monkeypatch, release)

    cancel_handles: list[app_module.CancelHandle] = []
    queues: list[queue.Queue[str]] = []
    subscribers: list[Iterator[object]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        cancel_handle = cancel_handles[0]
        input_queue.put("hello")
        time.sleep(0.1)
        cancel_handle.trigger()
        time.sleep(0.1)
        release.set()
        input_queue.put("world")
        time.sleep(0.2)

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle,
    ) -> None:
        queues.append(input_queue)
        cancel_handles.append(cancel_handle)
        subscribers.append(bus.subscribe())
        original_init(self, bus, input_queue, cancel_handle)

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path))

    subscriber = subscribers[0]
    seen: list[object] = []
    while sum(isinstance(event, RunFinished) for event in seen) < 1:
        seen.append(next(subscriber))

    assert seen[0] == RunStarted()
    cancelled_index = seen.index(RunCancelled())
    assert seen[cancelled_index + 1] == RunStarted()
    assert seen[-1] == RunFinished()


def test_run_pico_waits_for_core_thread_briefly_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(PicoApp, "run", noop_run)

    start = time.monotonic()
    run_pico(_config(tmp_path))
    elapsed = time.monotonic() - start

    assert elapsed < 2


def test_turn_persists_to_session_file_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = RecordingClient()

    def factory(*, model: str, base_url: str, api_key: str | None) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        deadline = time.monotonic() + 5
        while len(client.seen_messages) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
    ) -> None:
        queues.append(input_queue)
        original_init(self, bus, input_queue, cancel_handle)

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    config = _config(tmp_path)
    run_pico(config)

    conn = connect(config.session_path)
    session = Session(conn, DEFAULT_SESSION_ID)
    events = list(session.events())

    assert events == [
        UserMessageRecorded(content="hello"),
        AssistantMessageRecorded(content="hi", thinking=""),
    ]
