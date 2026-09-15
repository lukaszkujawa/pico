import queue
import threading

from pico.config import Config
from pico.core.agent import Run
from pico.core.bus import Bus
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.ollama import OllamaClient
from pico.llm.types import Message, Role
from pico.tui import PicoApp


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
    input_queue: "queue.Queue[str]",
    shutdown: threading.Event,
    cancel_handle: CancelHandle,
) -> None:
    run = Run(llm, tools, bus, [])
    while True:
        try:
            text = input_queue.get(timeout=0.1)
        except queue.Empty:
            if shutdown.is_set():
                return
            continue
        run.messages.append(Message(role=Role.USER, content=text))
        cancel = threading.Event()
        run.cancel = cancel
        cancel_handle.arm(cancel)
        run.execute()
        cancel_handle.disarm()


def run_pico(config: Config) -> None:
    llm = _build_llm_client(config)
    tools = ToolRegistry()
    bus = Bus()
    input_queue: queue.Queue[str] = queue.Queue()
    shutdown = threading.Event()
    cancel_handle = CancelHandle()

    core_thread = threading.Thread(
        target=_turn_loop,
        args=(llm, tools, bus, input_queue, shutdown, cancel_handle),
        daemon=True,
    )
    core_thread.start()

    try:
        PicoApp(bus, input_queue, cancel_handle).run()
    finally:
        shutdown.set()
        core_thread.join(timeout=1)
