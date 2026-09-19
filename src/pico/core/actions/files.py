from collections.abc import Mapping

from pico.core.actions.arguments import require
from pico.core.tools import Tool, ToolError
from pico.llm.types import ToolSpec

READ_FILE_CAP_CHARS = 4000

READ_FILE_SPEC = ToolSpec(
    name="read_file",
    description=(
        "Read the contents of a file at the given path. Pass offset (1-based start line) "
        "and limit (line count) to read a slice; without them, large files are truncated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    },
)


def read_file_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        offset = None if arguments.get("offset") is None else require(arguments, "offset", int)
        limit = None if arguments.get("limit") is None else require(arguments, "limit", int)
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError as error:
            raise ToolError(f"could not read {path}: {error}") from error
        if offset is None and limit is None:
            return content if len(content) <= READ_FILE_CAP_CHARS else _capped(content)
        return _slice(path, content, 1 if offset is None else offset, limit)

    return Tool(spec=READ_FILE_SPEC, execute=execute)


def _capped(content: str) -> str:
    lines = content.splitlines(keepends=True)
    kept = 0
    used = 0
    for line in lines:
        if used + len(line) > READ_FILE_CAP_CHARS:
            break
        used += len(line)
        kept += 1
    head = "".join(lines[:kept])
    return (
        f"{head}… {len(content)} chars / {len(lines)} lines total — "
        "pass offset and limit to read more"
    )


def _slice(path: str, content: str, offset: int, limit: int | None) -> str:
    lines = content.splitlines(keepends=True)
    total = len(lines)
    if offset < 1 or offset > total:
        raise ToolError(f"offset {offset} is out of range: {path} has {total} lines")
    if limit is not None and limit < 1:
        raise ToolError(f"limit must be at least 1, got {limit}")
    end = total if limit is None else min(offset - 1 + limit, total)
    return f"lines {offset}-{end} of {total}:\n" + "".join(lines[offset - 1 : end])


EDIT_FILE_SPEC = ToolSpec(
    name="edit_file",
    description=(
        "Replace one exact occurrence of old_text with new_text in a file. Use this when "
        "you know the file's exact current text because you just wrote or read it; "
        "delegate the edit when you do not."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    },
)


def edit_file_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        old_text = require(arguments, "old_text", str)
        new_text = require(arguments, "new_text", str)
        if old_text == new_text:
            raise ToolError("old_text and new_text are identical; nothing to change")
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError as error:
            raise ToolError(f"could not read {path}: {error}") from error
        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ToolError(f"old_text not found in {path}")
        if occurrences > 1:
            raise ToolError(
                f"old_text occurs {occurrences} times in {path}; "
                "add surrounding context to make it unique"
            )
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content.replace(old_text, new_text))
        except OSError as error:
            raise ToolError(f"could not write {path}: {error}") from error
        return f"edited {path}: replaced {len(old_text)} chars with {len(new_text)} chars"

    return Tool(spec=EDIT_FILE_SPEC, execute=execute)


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
