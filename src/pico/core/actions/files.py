from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from pico.core.actions.arguments import require
from pico.core.tools import Tool, ToolError
from pico.llm.types import ToolSpec

READ_FILE_SPEC = ToolSpec(
    name="read_file",
    description="Read the contents of a file at the given path.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


@dataclass(frozen=True)
class ReadFile:
    path: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        path = require(arguments, "path", str)
        return cls(path=path)

    def execute(self) -> str:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return handle.read()
        except OSError as error:
            raise ToolError(f"could not read {self.path}: {error}") from error


def read_file_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        return ReadFile.from_arguments(arguments).execute()

    return Tool(spec=READ_FILE_SPEC, execute=execute)


WRITE_FILE_SPEC = ToolSpec(
    name="write_file",
    description="Write content to a file at the given path, overwriting it if it exists.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
)


@dataclass(frozen=True)
class WriteFile:
    path: str
    content: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        path = require(arguments, "path", str)
        content = require(arguments, "content", str)
        return cls(path=path, content=content)

    def execute(self) -> str:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(self.content)
        except OSError as error:
            raise ToolError(f"could not write {self.path}: {error}") from error
        return f"wrote {len(self.content)} bytes to {self.path}"


def write_file_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        return WriteFile.from_arguments(arguments).execute()

    return Tool(spec=WRITE_FILE_SPEC, execute=execute)
