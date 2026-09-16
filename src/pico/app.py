import itertools
import queue
import threading

from pico.config import Config
from pico.core.actions import register_actions
from pico.core.bus import Bus
from pico.core.loop import DEFAULT_LOOP_CONFIG, LoopRunner
from pico.core.tools import ToolRegistry
from pico.debug.log import LoggingLLMClient, RunLog
from pico.llm.client import LLMClient
from pico.llm.ollama import OllamaClient
from pico.session import Session, UserMessageRecorded, connect
from pico.tui import PicoApp

DEFAULT_SESSION_ID = "default"


class UnsupportedVendorError(Exception):
    pass


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


def _build_llm_client(config: Config) -> LLMClient:
    if config.vendor != "ollama":
        raise UnsupportedVendorError(f"unsupported LLM vendor: {config.vendor}")
    return OllamaClient(model=config.model, base_url=config.base_url, api_key=config.api_key)


def _turn_loop(
    llm: LLMClient,
    tools: ToolRegistry,
    bus: Bus,
    session: Session,
    context_size: int,
    input_queue: "queue.Queue[str]",
    shutdown: threading.Event,
    cancel_handle: CancelHandle,
) -> None:
    id_source = itertools.count()
    while True:
        try:
            text = input_queue.get(timeout=0.1)
        except queue.Empty:
            if shutdown.is_set():
                return
            continue
        session.append(UserMessageRecorded(content=text))
        cancel = threading.Event()
        cancel_handle.arm(cancel)
        runner = LoopRunner(
            llm, tools, bus, session, context_size, DEFAULT_LOOP_CONFIG, cancel, id_source
        )
        runner.execute()
        cancel_handle.disarm()


def _consume_bus_to_log(bus: Bus, run_log: RunLog) -> None:
    for event in bus.subscribe():
        run_log.log(repr(event))


def run_pico(config: Config, debug: bool = False) -> None:
    llm = _build_llm_client(config)
    tools = ToolRegistry()
    register_actions(tools)
    bus = Bus()
    conn = connect(config.session_path)
    session = Session(conn, DEFAULT_SESSION_ID)
    input_queue: queue.Queue[str] = queue.Queue()
    shutdown = threading.Event()
    cancel_handle = CancelHandle()

    if debug:
        run_log = RunLog.create()
        run_log.log(
            f"vendor={config.vendor} model={config.model} context_size={config.context_size}"
        )
        llm = LoggingLLMClient(llm, run_log)
        threading.Thread(target=_consume_bus_to_log, args=(bus, run_log), daemon=True).start()

    core_thread = threading.Thread(
        target=_turn_loop,
        args=(llm, tools, bus, session, config.context_size, input_queue, shutdown, cancel_handle),
        daemon=True,
    )
    core_thread.start()

    try:
        PicoApp(bus, input_queue, cancel_handle).run()
    finally:
        shutdown.set()
        core_thread.join(timeout=1)
