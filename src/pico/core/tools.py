from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pico.llm.types import ToolCall, ToolSpec


class UnknownToolError(Exception):
    pass


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    execute: Callable[[Mapping[str, object]], str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def execute(self, call: ToolCall) -> str:
        tool = self._tools.get(call.name)
        if tool is None:
            raise UnknownToolError(call.name)
        return tool.execute(call.arguments)
