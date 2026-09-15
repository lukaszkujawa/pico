from collections.abc import Iterator
from typing import Protocol

from pico.llm.types import Message, StreamEvent, ToolSpec


class LLMClient(Protocol):
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]: ...
