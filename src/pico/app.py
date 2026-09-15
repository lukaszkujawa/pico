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


def _build_llm_client(config: Config) -> LLMClient:
    if config.vendor != "ollama":
        raise UnsupportedVendorError(f"unsupported LLM vendor: {config.vendor}")
    return OllamaClient(model=config.model, base_url=config.base_url, api_key=config.api_key)


def run_pico(config: Config) -> None:
    llm = _build_llm_client(config)
    tools = ToolRegistry()
    bus = Bus()
    messages = [Message(role=Role.USER, content="Hello, who are you?")]

    core_thread = threading.Thread(target=Run(llm, tools, bus, messages).execute, daemon=True)
    core_thread.start()

    try:
        PicoApp(bus).run()
    finally:
        core_thread.join(timeout=1)
