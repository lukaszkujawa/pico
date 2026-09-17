import os
import queue
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from textual.message import Message as TextualMessage

import pico.app as app_module
from pico.app import SessionHandle, UnsupportedVendorError, read_fifo, run_pico
from pico.config import Config, ConfigError
from pico.core.bus import Bus
from pico.core.context import SYSTEM_PROMPT
from pico.core.events import RunCancelled, RunFinished, RunStarted
from pico.llm.errors import LLMError
from pico.llm.types import GenerationComplete, Message, Role, StreamEvent, TextDelta, ToolSpec
from pico.session import (
    AssistantMessageRecorded,
    Session,
    UserMessageRecorded,
    connect,
    latest_session_id,
)
from pico.tui import PicoApp
from pico.tui.messages import UserInputSubmitted
from pico.tui.widgets import UserPane
from tests.conftest import settle, wait_until


class SlowClient:
    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield TextDelta(text="hi")
        self._release.wait(timeout=5)
        yield GenerationComplete(finish_reason="stop")


def _patch_ollama_client(monkeypatch: pytest.MonkeyPatch, release: threading.Event) -> None:
    def factory(*, model: str, base_url: str, api_key: str | None, context_size: int) -> SlowClient:
        return SlowClient(release)

    monkeypatch.setattr(app_module, "OllamaClient", factory)


def _config(tmp_path: Path, vendor: str = "ollama", context_size: int = 1024) -> Config:
    return Config(
        vendor=vendor,
        base_url="http://localhost:11434",
        model="qwen3",
        api_key=None,
        context_size=context_size,
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


@pytest.mark.parametrize("released_before_shutdown", [False, True])
def test_stopping_tui_does_not_leave_core_thread_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, released_before_shutdown: bool
) -> None:
    release = threading.Event()
    if released_before_shutdown:
        release.set()
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
        self.seen_tools: list[list[ToolSpec]] = []
        self.spoken = False

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        if self.spoken:
            self.spoken = False
            raise LLMError("connection lost")
        self.spoken = True
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools))
        yield TextDelta(text="hi")
        yield GenerationComplete(finish_reason="stop")


def test_turn_loop_runs_one_turn_per_queued_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = RecordingClient()

    def factory(
        *, model: str, base_url: str, api_key: str | None, context_size: int
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the first turn reaches the client")
        input_queue.put("world")
        wait_until(lambda: len(client.seen_messages) >= 2, "the second turn reaches the client")

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        queues.append(input_queue)
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path, context_size=8192))

    assert len(client.seen_messages) == 2
    assert [m.content for m in client.seen_messages[0]] == [SYSTEM_PROMPT, "hello"]
    assert [m.content for m in client.seen_messages[1]] == [SYSTEM_PROMPT, "hello", "hi", "world"]
    assert [m.role for m in client.seen_messages[1]] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    assert {spec.name for spec in client.seen_tools[0]} == {
        "read_file",
        "write_file",
        "shell",
        "load_table",
        "sql",
        "note",
        "search_facts",
        "read_fact",
        "set_plan",
        "complete_step",
        "answer",
        "delegate",
    }


def test_cancelling_mid_turn_stops_run_and_allows_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    _patch_ollama_client(monkeypatch, release)

    cancel_handles: list[app_module.CancelHandle] = []
    queues: list[queue.Queue[str]] = []
    seen: list[object] = []

    def count(event_type: type[object]) -> int:
        return sum(isinstance(event, event_type) for event in list(seen))

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        cancel_handle = cancel_handles[0]
        input_queue.put("hello")
        wait_until(lambda: count(RunStarted) == 1, "the first turn starts")
        cancel_handle.trigger()
        release.set()
        wait_until(lambda: count(RunCancelled) == 1, "the first turn is cancelled")
        input_queue.put("world")
        wait_until(lambda: count(RunFinished) == 1, "the second turn finishes")

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        queues.append(input_queue)
        cancel_handles.append(cancel_handle)
        subscriber = bus.subscribe()
        threading.Thread(target=lambda: seen.extend(subscriber), daemon=True).start()
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path))

    assert seen[0] == RunStarted()
    cancelled_index = seen.index(RunCancelled())
    assert seen[cancelled_index + 1] == RunStarted()
    assert seen[-1] == RunFinished()


def test_turn_persists_to_session_file_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = RecordingClient()

    def factory(
        *, model: str, base_url: str, api_key: str | None, context_size: int
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        queues.append(input_queue)
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    config = _config(tmp_path)
    run_pico(config)

    conn = connect(config.session_path)
    session_id = latest_session_id(conn)
    assert session_id is not None
    session = Session(conn, session_id)
    events = list(session.events())

    assert events == [
        UserMessageRecorded(content="hello"),
        AssistantMessageRecorded(content="hi", thinking=""),
    ]


def test_debug_true_writes_run_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = RecordingClient()

    def factory(
        *, model: str, base_url: str, api_key: str | None, context_size: int
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)
    monkeypatch.chdir(tmp_path)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        queues.append(input_queue)
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), debug=True)

    logs_root = tmp_path / "logs"
    assert logs_root.is_dir()
    run_dirs = list(logs_root.iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "prompt-1.txt").exists()
    assert (run_dir / "resp-1.txt").exists()
    assert (run_dir / "session.log").exists()


def test_debug_false_creates_no_logs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client = RecordingClient()

    def factory(
        *, model: str, base_url: str, api_key: str | None, context_size: int
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)
    monkeypatch.chdir(tmp_path)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        queues.append(input_queue)
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), debug=False)

    assert not (tmp_path / "logs").exists()


def _run_one_turn(monkeypatch: pytest.MonkeyPatch, config: Config, session_id: str | None) -> None:
    client = RecordingClient()

    def factory(
        *, model: str, base_url: str, api_key: str | None, context_size: int
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        queues[0].put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        queues.append(input_queue)
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(config, session_id=session_id)


def test_each_run_starts_a_fresh_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)

    _run_one_turn(monkeypatch, config, session_id=None)
    first_id = latest_session_id(connect(config.session_path))

    _run_one_turn(monkeypatch, config, session_id=None)
    conn = connect(config.session_path)
    session_ids = {row[0] for row in conn.execute("SELECT DISTINCT session_id FROM events")}

    assert first_id is not None
    assert len(session_ids) == 2
    assert first_id in session_ids

    other_id = next(iter(session_ids - {first_id}))
    assert Session(conn, first_id).messages() == Session(conn, other_id).messages()
    assert len(Session(conn, first_id).messages()) == 2


def test_explicit_session_id_resumes_existing_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)

    _run_one_turn(monkeypatch, config, session_id=None)
    conn = connect(config.session_path)
    resumed_id = latest_session_id(conn)
    assert resumed_id is not None

    _run_one_turn(monkeypatch, config, session_id=resumed_id)

    session_ids = {row[0] for row in conn.execute("SELECT DISTINCT session_id FROM events")}
    assert session_ids == {resumed_id}
    assert len(Session(conn, resumed_id).messages()) == 4


def test_session_handle_start_new_switches_to_an_empty_session(tmp_path: Path) -> None:
    conn = connect(tmp_path / "session.db")
    original = Session(conn, "original")
    original.append(UserMessageRecorded(content="hello"))
    handle = SessionHandle(original)

    handle.start_new()

    assert handle.session_id != "original"
    assert handle.session.messages() == []
    assert len(original.messages()) == 1


def test_read_fifo_delivers_lines_in_order_across_writers_and_skips_blanks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pico.sock"
    os.mkfifo(path)
    shutdown = threading.Event()
    lines: list[str] = []
    reader = threading.Thread(target=read_fifo, args=(str(path), lines.append, shutdown))
    reader.start()

    with open(path, "w") as writer:
        writer.write("first\nsecond\n")
    wait_until(lambda: lines == ["first", "second"], "both lines are delivered")

    with open(path, "w") as writer:
        writer.write("   \n\nthird\n")
    wait_until(lambda: lines == ["first", "second", "third"], "the post-EOF line is delivered")

    shutdown.set()
    reader.join(timeout=5)
    assert not reader.is_alive()


async def test_fifo_line_behaves_like_typed_input(tmp_path: Path) -> None:
    path = tmp_path / "pico.sock"
    os.mkfifo(path)
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue)
    shutdown = threading.Event()

    def submit(text: str) -> None:
        app.post_message(UserInputSubmitted(text=text))

    reader = threading.Thread(target=read_fifo, args=(str(path), submit, shutdown), daemon=True)
    try:
        async with app.run_test() as pilot:
            reader.start()
            with open(path, "w") as writer:
                writer.write("hello\nworld\n")
            await settle(pilot, lambda: len(app.query(UserPane)) == 2, "both user panes appear")
            panes = list(app.query(UserPane))
            assert [pane.render().plain for pane in panes] == ["hello", "world  queued"]
            assert [pane.queued for pane in panes] == [False, True]
            assert input_queue.get(timeout=5) == "hello"
            assert input_queue.get(timeout=5) == "world"
    finally:
        shutdown.set()
    reader.join(timeout=5)
    assert not reader.is_alive()


def test_run_pico_wires_the_fifo_to_the_app_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    sock = str(tmp_path / "pico.sock")
    submitted: list[str] = []

    def capturing_post_message(self: PicoApp, message: TextualMessage) -> bool:
        if isinstance(message, UserInputSubmitted):
            submitted.append(message.text)
        return True

    threads: list[threading.Thread] = []
    original_thread_init = threading.Thread.__init__

    def tracking_init(self: threading.Thread, *args: object, **kwargs: object) -> None:
        original_thread_init(self, *args, **kwargs)  # type: ignore[arg-type]
        threads.append(self)

    def driving_run(self: PicoApp) -> None:
        assert stat.S_ISFIFO(os.stat(sock).st_mode)
        with open(sock, "w") as writer:
            writer.write("hello from fifo\n")
        wait_until(lambda: submitted == ["hello from fifo"], "the line reaches the app")

    monkeypatch.setattr(PicoApp, "post_message", capturing_post_message)
    monkeypatch.setattr(threading.Thread, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), sock=sock)

    assert submitted == ["hello from fifo"]
    assert not Path(sock).exists()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_run_pico_fails_before_starting_threads_when_sock_path_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sock = tmp_path / "pico.sock"
    sock.write_text("not a fifo")
    started: list[bool] = []

    def tracking_start(self: threading.Thread) -> None:
        started.append(True)

    monkeypatch.setattr(threading.Thread, "start", tracking_start)

    with pytest.raises(ConfigError, match="cannot create FIFO"):
        run_pico(_config(tmp_path), sock=str(sock))

    assert started == []
    assert sock.read_text() == "not a fifo"


def test_run_pico_without_sock_creates_no_fifo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(PicoApp, "run", noop_run)

    run_pico(_config(tmp_path))

    assert not any(stat.S_ISFIFO(entry.stat().st_mode) for entry in tmp_path.iterdir())


def test_run_pico_hands_the_configured_context_size_to_the_tui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    seen_context_sizes: list[int] = []
    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
    ) -> None:
        seen_context_sizes.append(context_size)
        original_init(
            self, bus, input_queue, cancel_handle, session_handle, initial_prompt, context_size
        )

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", noop_run)

    run_pico(_config(tmp_path, context_size=4096))

    assert seen_context_sizes == [4096]
