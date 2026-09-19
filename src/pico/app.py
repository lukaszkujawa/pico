import itertools
import json
import os
import queue
import threading
from collections.abc import Callable

from pico.config import Config, ConfigError
from pico.core.actions import register_actions
from pico.core.bus import Bus
from pico.core.events import AnswerSettled, RunCancelled, RunFinished, RunStarted
from pico.core.loop import DEFAULT_LOOP_CONFIG, LoopRunner
from pico.core.tools import ToolRegistry
from pico.debug.log import LoggingLLMClient, RunLog
from pico.llm.anthropic import AnthropicClient
from pico.llm.client import LLMClient, LLMError
from pico.llm.ollama import OllamaClient
from pico.llm.openai import OpenAIClient
from pico.session import Session, UserMessageRecorded, connect, new_session_id
from pico.tui import PicoApp
from pico.tui.commands import Options
from pico.tui.messages import UserInputSubmitted


class UnsupportedVendorError(Exception):
    pass


class SessionHandle:
    def __init__(self, session: Session) -> None:
        self._lock = threading.Lock()
        self._session = session

    @property
    def session(self) -> Session:
        with self._lock:
            return self._session

    @property
    def session_id(self) -> str:
        return self.session.session_id

    def start_new(self) -> None:
        session = Session(self.session.connection, new_session_id())
        with self._lock:
            self._session = session


class LLMHandle:
    def __init__(self, client: LLMClient, run_log: RunLog | None = None) -> None:
        self._lock = threading.Lock()
        self._run_log = run_log
        self._client = self._wrap(client)

    def _wrap(self, client: LLMClient) -> LLMClient:
        if self._run_log is None:
            return client
        return LoggingLLMClient(client, self._run_log)

    @property
    def client(self) -> LLMClient:
        with self._lock:
            return self._client

    def switch(self, client: LLMClient) -> None:
        wrapped = self._wrap(client)
        with self._lock:
            self._client = wrapped


class ModelSwitch:
    def __init__(self, config: Config, llm_handle: LLMHandle) -> None:
        self._config = config
        self._llm_handle = llm_handle
        self._current = config.model

    @property
    def current(self) -> str:
        return self._current

    def available(self) -> Options:
        try:
            return Options(names=tuple(self._llm_handle.client.models()))
        except LLMError as error:
            return Options(error=str(error))

    def switch_to(self, model: str) -> None:
        self._llm_handle.switch(build_llm_client(self._config, model))
        self._current = model


class CancelHandle:
    def __init__(self) -> None:
        self._event: threading.Event | None = None

    def arm(self, event: threading.Event) -> None:
        self._event = event

    def disarm(self) -> None:
        self._event = None

    def trigger(self) -> None:
        if self._event is not None:
            self._event.set()


def build_llm_client(config: Config, model: str | None = None) -> LLMClient:
    vendor_clients: dict[str, type[OllamaClient] | type[OpenAIClient] | type[AnthropicClient]] = {
        "ollama": OllamaClient,
        "openai": OpenAIClient,
        "anthropic": AnthropicClient,
    }
    client_class = vendor_clients.get(config.vendor)
    if client_class is None:
        raise UnsupportedVendorError(f"unsupported LLM vendor: {config.vendor}")
    return client_class(
        model=model or config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        context_size=config.context_size,
        temperature=config.temperature,
    )


def _turn_loop(
    llm_handle: LLMHandle,
    bus: Bus,
    session_handle: SessionHandle,
    context_size: int,
    input_queue: "queue.Queue[str]",
    shutdown: threading.Event,
    cancel_handle: CancelHandle,
    vision: bool,
) -> None:
    id_source = itertools.count()
    while True:
        try:
            text = input_queue.get(timeout=0.1)
        except queue.Empty:
            if shutdown.is_set():
                return
            continue
        llm = llm_handle.client
        session = session_handle.session
        session.append(UserMessageRecorded(content=text))
        tools = ToolRegistry()
        register_actions(tools, session, vision=vision)
        cancel = threading.Event()
        cancel_handle.arm(cancel)
        runner = LoopRunner(
            llm, tools, bus, session, context_size, DEFAULT_LOOP_CONFIG, cancel, id_source
        )
        runner.execute()
        cancel_handle.disarm()


def _create_mailbox_file(path: str) -> None:
    try:
        with open(path, "a"):
            pass
    except OSError as error:
        raise ConfigError(f"cannot create mailbox file at {path}: {error}") from error


def open_inbox(path: str) -> int:
    fd = os.open(path, os.O_RDONLY)
    os.lseek(fd, 0, os.SEEK_END)
    return fd


def read_inbox(fd: int, submit: Callable[[str], None], shutdown: threading.Event) -> None:
    buffer = b""
    try:
        while not shutdown.is_set():
            chunk = os.read(fd, 4096)
            if len(chunk) == 0:
                shutdown.wait(0.1)
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                text = line.decode()
                if text.strip():
                    submit(text)
    finally:
        os.close(fd)


def _write_reply(path: str, answer: str | None, cause: str | None) -> None:
    if answer is not None:
        record: dict[str, str | None] = {"status": "answered", "content": answer, "reason": None}
    else:
        record = {"status": "stopped", "content": None, "reason": cause or "no answer"}
    with open(path, "a") as outbox:
        outbox.write(json.dumps(record) + "\n")


def write_outbox(path: str, bus: Bus, shutdown: threading.Event) -> None:
    answer: str | None = None
    for event in bus.subscribe():
        if shutdown.is_set():
            return
        match event:
            case RunStarted():
                answer = None
            case AnswerSettled(content=content, accepted=True, complete=True):
                answer = content
            case RunFinished(error=error):
                _write_reply(path, answer, error)
                answer = None
            case RunCancelled():
                _write_reply(path, answer, "cancelled")
                answer = None
            case _:
                pass


def _consume_bus_to_log(bus: Bus, run_log: RunLog) -> None:
    for event in bus.subscribe():
        run_log.log(repr(event))


def run_pico(
    config: Config,
    debug: bool = False,
    session_id: str | None = None,
    initial_prompt: str | None = None,
    mailbox: str | None = None,
) -> None:
    client = build_llm_client(config)
    if mailbox is not None:
        _create_mailbox_file(mailbox)
        _create_mailbox_file(mailbox + ".out")
    bus = Bus()
    conn = connect(config.session_path)
    session_handle = SessionHandle(Session(conn, session_id or new_session_id()))
    input_queue: queue.Queue[str] = queue.Queue()
    shutdown = threading.Event()
    cancel_handle = CancelHandle()
    run_log: RunLog | None = None

    if debug:
        run_log = RunLog.create()
        run_log.log(
            f"vendor={config.vendor} model={config.model} context_size={config.context_size}"
        )
        threading.Thread(target=_consume_bus_to_log, args=(bus, run_log), daemon=True).start()

    llm_handle = LLMHandle(client, run_log)

    core_thread = threading.Thread(
        target=_turn_loop,
        args=(
            llm_handle,
            bus,
            session_handle,
            config.context_size,
            input_queue,
            shutdown,
            cancel_handle,
            config.vision,
        ),
        daemon=True,
    )
    core_thread.start()

    app = PicoApp(
        bus,
        input_queue,
        cancel_handle,
        session_handle,
        initial_prompt,
        config.context_size,
        DEFAULT_LOOP_CONFIG.max_steps,
        ModelSwitch(config, llm_handle),
    )

    reader_thread: threading.Thread | None = None
    writer_thread: threading.Thread | None = None
    if mailbox is not None:

        def submit(text: str) -> None:
            app.post_message(UserInputSubmitted(text=text))

        reader_thread = threading.Thread(
            target=read_inbox, args=(open_inbox(mailbox), submit, shutdown), daemon=True
        )
        reader_thread.start()
        writer_thread = threading.Thread(
            target=write_outbox, args=(mailbox + ".out", bus, shutdown), daemon=True
        )
        writer_thread.start()

    try:
        app.run()
    finally:
        shutdown.set()
        bus.close()
        core_thread.join(timeout=1)
        if reader_thread is not None:
            reader_thread.join(timeout=1)
        if writer_thread is not None:
            writer_thread.join(timeout=1)
        if mailbox is not None:
            os.unlink(mailbox)
            os.unlink(mailbox + ".out")
