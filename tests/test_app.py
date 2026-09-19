import json
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from textual.message import Message as TextualMessage

import pico.app as app_module
from pico.app import (
    LLMHandle,
    SessionHandle,
    UnsupportedVendorError,
    build_llm_client,
    open_inbox,
    read_inbox,
    run_pico,
    write_outbox,
)
from pico.app import ModelSwitch as ModelSwitchImpl
from pico.config import Config, ConfigError
from pico.core.bus import Bus
from pico.core.events import AnswerSettled, RunCancelled, RunFinished, RunStarted
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.prompt import SYSTEM_PROMPT
from pico.debug.log import LoggingLLMClient, RunLog
from pico.llm.anthropic import AnthropicClient
from pico.llm.client import LLMError
from pico.llm.ollama import OllamaClient
from pico.llm.openai import OpenAIClient
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)
from pico.session import (
    AssistantMessageRecorded,
    Session,
    UserMessageRecorded,
    connect,
    latest_session_id,
)
from pico.tui import PicoApp
from pico.tui.app import ModelSwitch
from pico.tui.commands import Options
from pico.tui.messages import UserInputSubmitted
from pico.tui.widgets import SystemPane, UserPane
from tests.conftest import settle, wait_until
from tests.llm_fakes import NoModels


class SlowClient(NoModels):
    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield TextDelta(text="hi")
        self._release.wait(timeout=5)
        yield GenerationComplete(finish_reason="stop")


def _patch_ollama_client(monkeypatch: pytest.MonkeyPatch, release: threading.Event) -> None:
    def factory(
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
    ) -> SlowClient:
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


@dataclass
class AppSpy:
    events: list[object] = field(default_factory=list[object])
    queues: list["queue.Queue[str]"] = field(default_factory=list["queue.Queue[str]"])
    cancel_handles: list[app_module.CancelHandle] = field(
        default_factory=list[app_module.CancelHandle]
    )
    context_sizes: list[int] = field(default_factory=list[int])
    max_steps: list[int | None] = field(default_factory=list[int | None])
    model_switches: list[ModelSwitch | None] = field(default_factory=list["ModelSwitch | None"])


def _spy_on_app_init(monkeypatch: pytest.MonkeyPatch) -> AppSpy:
    spy = AppSpy()
    original_init = PicoApp.__init__

    def tracking_init(
        self: PicoApp,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: app_module.CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
        initial_prompt: str | None = None,
        context_size: int = 8192,
        max_steps: int | None = None,
        model_switch: ModelSwitch | None = None,
    ) -> None:
        subscriber = bus.subscribe()
        threading.Thread(target=lambda: spy.events.extend(subscriber), daemon=True).start()
        spy.queues.append(input_queue)
        if cancel_handle is not None:
            spy.cancel_handles.append(cancel_handle)
        spy.context_sizes.append(context_size)
        spy.max_steps.append(max_steps)
        spy.model_switches.append(model_switch)
        original_init(
            self,
            bus,
            input_queue,
            cancel_handle,
            session_handle,
            initial_prompt,
            context_size,
            max_steps,
            model_switch,
        )

    monkeypatch.setattr(PicoApp, "__init__", tracking_init)
    return spy


def test_build_llm_client_builds_the_vendor_client(tmp_path: Path) -> None:
    assert isinstance(build_llm_client(_config(tmp_path)), OllamaClient)
    assert isinstance(build_llm_client(_config(tmp_path, vendor="openai")), OpenAIClient)
    assert isinstance(build_llm_client(_config(tmp_path, vendor="anthropic")), AnthropicClient)


def test_build_llm_client_rejects_unknown_vendors(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedVendorError, match="mystery"):
        build_llm_client(_config(tmp_path, vendor="mystery"))


def test_unsupported_vendor_raises_before_starting_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[bool] = []

    def tracking_start(self: threading.Thread) -> None:
        started.append(True)

    monkeypatch.setattr(threading.Thread, "start", tracking_start)

    with pytest.raises(UnsupportedVendorError):
        run_pico(_config(tmp_path, vendor="mystery"))

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


class RecordingClient(NoModels):
    def __init__(self) -> None:
        self.seen_messages: list[list[Message]] = []
        self.seen_tools: list[list[ToolSpec]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools))
        yield TextDelta(text="hi")
        yield GenerationComplete(finish_reason="stop")


def test_turn_loop_runs_one_turn_per_queued_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = RecordingClient()

    def factory(
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
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

    queues = _spy_on_app_init(monkeypatch).queues
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
        "edit_file",
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

    spy = _spy_on_app_init(monkeypatch)
    queues, cancel_handles, seen = spy.queues, spy.cancel_handles, spy.events

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
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    queues = _spy_on_app_init(monkeypatch).queues
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
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)
    monkeypatch.chdir(tmp_path)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    queues = _spy_on_app_init(monkeypatch).queues
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
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)
    monkeypatch.chdir(tmp_path)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        input_queue = queues[0]
        input_queue.put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    queues = _spy_on_app_init(monkeypatch).queues
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), debug=False)

    assert not (tmp_path / "logs").exists()


def _run_one_turn(monkeypatch: pytest.MonkeyPatch, config: Config, session_id: str | None) -> None:
    client = RecordingClient()

    def factory(
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
    ) -> RecordingClient:
        return client

    monkeypatch.setattr(app_module, "OllamaClient", factory)

    queues: list[queue.Queue[str]] = []

    def driving_run(self: PicoApp) -> None:
        queues[0].put("hello")
        wait_until(lambda: len(client.seen_messages) >= 1, "the turn reaches the client")

    queues = _spy_on_app_init(monkeypatch).queues
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


def test_read_inbox_delivers_appended_lines_in_order_and_skips_blanks_and_backlog(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker"
    path.write_text("stale backlog\n")
    shutdown = threading.Event()
    lines: list[str] = []
    reader = threading.Thread(
        target=read_inbox, args=(open_inbox(str(path)), lines.append, shutdown)
    )
    reader.start()

    with open(path, "a") as writer:
        writer.write("first\nsecond\n")
    wait_until(lambda: lines == ["first", "second"], "both lines are delivered")

    with open(path, "a") as writer:
        writer.write("   \n\nthird\n")
    wait_until(lambda: lines == ["first", "second", "third"], "the later append is delivered")

    shutdown.set()
    reader.join(timeout=5)
    assert not reader.is_alive()


def test_read_inbox_holds_a_partial_line_until_its_newline_arrives(tmp_path: Path) -> None:
    path = tmp_path / "worker"
    path.touch()
    shutdown = threading.Event()
    lines: list[str] = []
    reader = threading.Thread(
        target=read_inbox, args=(open_inbox(str(path)), lines.append, shutdown)
    )
    reader.start()

    with open(path, "a") as writer:
        writer.write("partial")
    with open(path, "a") as writer:
        writer.write(" line\n")
    wait_until(lambda: lines == ["partial line"], "the completed line is delivered")

    shutdown.set()
    reader.join(timeout=5)
    assert not reader.is_alive()


def _start_reply_writer(path: Path) -> tuple[Bus, threading.Thread]:
    bus = Bus()
    writer = threading.Thread(
        target=write_outbox, args=(str(path), bus, threading.Event()), daemon=True
    )
    writer.start()
    return bus, writer


def _replies(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text().splitlines()


def _finish_reply_writer(bus: Bus, writer: threading.Thread) -> None:
    bus.close()
    writer.join(timeout=5)
    assert not writer.is_alive()


def test_write_outbox_emits_one_answered_record_with_newlines_preserved(tmp_path: Path) -> None:
    outbox = tmp_path / "reply.out"
    bus, writer = _start_reply_writer(outbox)

    bus.publish(RunStarted())
    bus.publish(AnswerSettled(id="1", content="line one\nline two", accepted=True))
    bus.publish(RunFinished())
    wait_until(lambda: len(_replies(outbox)) == 1, "the reply record arrives")

    assert json.loads(_replies(outbox)[0]) == {
        "status": "answered",
        "content": "line one\nline two",
        "reason": None,
    }
    _finish_reply_writer(bus, writer)


@pytest.mark.parametrize(
    ("turn_end", "reason"),
    [
        (RunFinished(error="run stopped: the budget is spent"), "run stopped: the budget is spent"),
        (RunFinished(), "no answer"),
        (RunCancelled(), "cancelled"),
    ],
)
def test_write_outbox_emits_a_stopped_record_when_no_answer_is_accepted(
    tmp_path: Path, turn_end: RunFinished | RunCancelled, reason: str
) -> None:
    outbox = tmp_path / "reply.out"
    bus, writer = _start_reply_writer(outbox)

    bus.publish(RunStarted())
    bus.publish(AnswerSettled(id="1", content="rejected", accepted=False, reason="needs work"))
    bus.publish(AnswerSettled(id="2", content="partial", accepted=True, complete=False))
    bus.publish(turn_end)
    wait_until(lambda: len(_replies(outbox)) == 1, "the reply record arrives")

    assert json.loads(_replies(outbox)[0]) == {
        "status": "stopped",
        "content": None,
        "reason": reason,
    }
    _finish_reply_writer(bus, writer)


def test_write_outbox_does_not_carry_an_answer_into_the_next_turn(tmp_path: Path) -> None:
    outbox = tmp_path / "reply.out"
    bus, writer = _start_reply_writer(outbox)

    bus.publish(RunStarted())
    bus.publish(AnswerSettled(id="1", content="first", accepted=True))
    bus.publish(RunFinished())
    bus.publish(RunStarted())
    bus.publish(RunFinished(error="boom"))
    wait_until(lambda: len(_replies(outbox)) == 2, "both reply records arrive")

    lines = _replies(outbox)
    assert json.loads(lines[0]) == {"status": "answered", "content": "first", "reason": None}
    assert json.loads(lines[1]) == {"status": "stopped", "content": None, "reason": "boom"}
    _finish_reply_writer(bus, writer)


def test_run_pico_writes_one_reply_per_turn_to_the_outbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_named_clients(monkeypatch)
    spy = _spy_on_app_init(monkeypatch)
    mailbox = str(tmp_path / "worker")
    outbox = tmp_path / "worker.out"
    lines: list[str] = []

    def driving_run(self: PicoApp) -> None:
        assert outbox.is_file()
        spy.queues[0].put("hello")
        wait_until(lambda: len(_replies(outbox)) == 1, "the reply record arrives")
        lines.extend(_replies(outbox))

    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), mailbox=mailbox)

    assert json.loads(lines[0]) == {"status": "answered", "content": "qwen3", "reason": None}
    assert not Path(mailbox).exists()
    assert not outbox.exists()


async def test_inbox_line_behaves_like_typed_input(tmp_path: Path) -> None:
    path = tmp_path / "worker"
    path.touch()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue)
    shutdown = threading.Event()

    def submit(text: str) -> None:
        app.post_message(UserInputSubmitted(text=text))

    reader = threading.Thread(
        target=read_inbox, args=(open_inbox(str(path)), submit, shutdown), daemon=True
    )
    try:
        async with app.run_test() as pilot:
            reader.start()
            with open(path, "a") as writer:
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


async def test_inbox_line_starting_with_a_slash_is_a_prompt_not_a_command(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker"
    path.touch()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue)
    shutdown = threading.Event()

    def submit(text: str) -> None:
        app.post_message(UserInputSubmitted(text=text))

    reader = threading.Thread(
        target=read_inbox, args=(open_inbox(str(path)), submit, shutdown), daemon=True
    )
    try:
        async with app.run_test() as pilot:
            reader.start()
            with open(path, "a") as writer:
                writer.write("/work/scheduler.py\n")
            await settle(pilot, lambda: len(app.query(UserPane)) == 1, "the user pane appears")
            assert not app.query(SystemPane)
            assert input_queue.get(timeout=5) == "/work/scheduler.py"
    finally:
        shutdown.set()
    reader.join(timeout=5)
    assert not reader.is_alive()


def test_run_pico_wires_the_inbox_to_the_app_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    mailbox = str(tmp_path / "worker")
    Path(mailbox).write_text("stale backlog\n")
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
        assert Path(mailbox).is_file()
        with open(mailbox, "a") as writer:
            writer.write("hello from inbox\n")
        wait_until(lambda: submitted == ["hello from inbox"], "the line reaches the app")

    monkeypatch.setattr(PicoApp, "post_message", capturing_post_message)
    monkeypatch.setattr(threading.Thread, "__init__", tracking_init)
    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), mailbox=mailbox)

    assert submitted == ["hello from inbox"]
    assert not Path(mailbox).exists()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_run_pico_fails_before_starting_threads_when_mailbox_dir_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[bool] = []

    def tracking_start(self: threading.Thread) -> None:
        started.append(True)

    monkeypatch.setattr(threading.Thread, "start", tracking_start)

    with pytest.raises(ConfigError, match="cannot create mailbox file"):
        run_pico(_config(tmp_path), mailbox=str(tmp_path / "missing" / "worker"))

    assert started == []


def test_run_pico_without_mailbox_creates_no_mailbox_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(PicoApp, "run", noop_run)

    run_pico(_config(tmp_path))

    assert all(entry.name.startswith("session.db") for entry in tmp_path.iterdir())


def test_run_pico_hands_the_configured_context_size_to_the_tui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    release.set()
    _patch_ollama_client(monkeypatch, release)

    spy = _spy_on_app_init(monkeypatch)

    def noop_run(self: PicoApp) -> None:
        return None

    monkeypatch.setattr(PicoApp, "run", noop_run)

    run_pico(_config(tmp_path, context_size=4096))

    assert spy.context_sizes == [4096]
    assert spy.max_steps == [DEFAULT_LOOP_CONFIG.budget(0)]


class NamedClient(NoModels):
    def __init__(self, model: str) -> None:
        self.model = model
        self.calls = 0

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.calls += 1
        yield ToolCallReady(
            tool_call=ToolCall(
                id="a", name="answer", arguments={"content": self.model, "citations": []}
            )
        )
        yield GenerationComplete(finish_reason="tool_calls")


def _patch_named_clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, NamedClient]:
    built: dict[str, NamedClient] = {}

    def factory(
        *,
        model: str,
        base_url: str,
        api_key: str | None,
        context_size: int,
        temperature: float | None,
    ) -> NamedClient:
        built[model] = NamedClient(model)
        return built[model]

    monkeypatch.setattr(app_module, "OllamaClient", factory)
    return built


def test_llm_handle_serves_the_replacement_client_after_a_switch() -> None:
    first, second = NamedClient("a"), NamedClient("b")
    handle = LLMHandle(first)

    assert handle.client is first

    handle.switch(second)

    assert handle.client is second


def test_llm_handle_rewraps_every_replacement_with_the_run_log(tmp_path: Path) -> None:
    run_log = RunLog(tmp_path)
    handle = LLMHandle(NamedClient("a"), run_log)

    assert isinstance(handle.client, LoggingLLMClient)

    handle.switch(NamedClient("b"))

    assert isinstance(handle.client, LoggingLLMClient)
    list(handle.client.stream([], []))
    assert (tmp_path / "prompt-1.txt").exists()


def test_model_switch_lists_available_models_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Listing(NoModels):
        def __init__(self, names: list[str] | None) -> None:
            self._names = names

        def models(self) -> list[str]:
            if self._names is None:
                raise LLMError("connection refused")
            return self._names

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            yield GenerationComplete(finish_reason="stop")

    config = _config(tmp_path)
    working = ModelSwitchImpl(config, LLMHandle(Listing(["a", "b"])))
    broken = ModelSwitchImpl(config, LLMHandle(Listing(None)))

    assert working.available() == Options(names=("a", "b"))
    assert working.current == "qwen3"
    assert broken.available() == Options(error="connection refused")


def test_model_switch_builds_a_replacement_client_with_only_the_model_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = _patch_named_clients(monkeypatch)
    config = _config(tmp_path)
    handle = LLMHandle(build_llm_client(config))
    switch = ModelSwitchImpl(config, handle)

    switch.switch_to("gemma3:27b")

    assert switch.current == "gemma3:27b"
    assert built["gemma3:27b"] is handle.client


def test_switching_models_moves_the_next_run_to_the_new_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = _patch_named_clients(monkeypatch)
    spy = _spy_on_app_init(monkeypatch)

    def driving_run(self: PicoApp) -> None:
        queue_ = spy.queues[0]
        switch = spy.model_switches[0]
        assert switch is not None
        queue_.put("hello")
        wait_until(lambda: built["qwen3"].calls == 1, "the first run uses the configured model")
        switch.switch_to("gemma3:27b")
        queue_.put("again")
        wait_until(lambda: built["gemma3:27b"].calls == 1, "the next run uses the new model")

    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path))

    assert built["qwen3"].calls == 1
    assert built["gemma3:27b"].calls == 1


def test_runs_after_a_switch_still_write_to_the_same_run_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = _patch_named_clients(monkeypatch)
    spy = _spy_on_app_init(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def driving_run(self: PicoApp) -> None:
        switch = spy.model_switches[0]
        assert switch is not None
        switch.switch_to("gemma3:27b")
        spy.queues[0].put("hello")
        wait_until(lambda: built["gemma3:27b"].calls == 1, "the run uses the new model")

    monkeypatch.setattr(PicoApp, "run", driving_run)

    run_pico(_config(tmp_path), debug=True)

    run_dir = next(iter((tmp_path / "logs").iterdir()))
    assert (run_dir / "prompt-1.txt").exists()
    assert (run_dir / "resp-1.txt").exists()
