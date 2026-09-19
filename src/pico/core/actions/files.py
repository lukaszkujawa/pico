from collections.abc import Mapping

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


def read_file_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        except OSError as error:
            raise ToolError(f"could not read {path}: {error}") from error

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


def write_file_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        content = require(arguments, "content", str)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as error:
            raise ToolError(f"could not write {path}: {error}") from error
        return f"wrote {len(content)} bytes to {path}"

    return Tool(spec=WRITE_FILE_SPEC, execute=execute)
