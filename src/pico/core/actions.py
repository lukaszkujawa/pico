import codecs
import contextlib
import json
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, Literal, Self, cast

from pico.core.errors import ToolError
from pico.core.ledger import facts, plan, render_plan
from pico.core.scratch import Scratch, load_table, query
from pico.core.tools import Tool, ToolRegistry
from pico.llm.types import ToolSpec
from pico.session import PlanSet, PlanStepCompleted, Session

MAX_DELEGATE_DEPTH = 3


class InvalidActionError(ValueError):
    pass


def require[T](arguments: Mapping[str, object], field: str, expected: type[T]) -> T:
    if field not in arguments:
        raise InvalidActionError(f"missing required field {field!r}")
    value = arguments[field]
    if not isinstance(value, expected):
        raise InvalidActionError(
            f"field {field!r} must be a {expected.__name__}, got {type(value).__name__}"
        )
    return value


def _require_str_list(arguments: Mapping[str, object], field: str) -> tuple[str, ...]:
    if field not in arguments:
        raise InvalidActionError(f"missing required field {field!r}")
    value = arguments[field]
    if not isinstance(value, list):
        raise InvalidActionError(f"field {field!r} must be a list, got {type(value).__name__}")
    raw = cast(list[object], value)
    elements: list[str] = []
    for element in raw:
        if not isinstance(element, str):
            raise InvalidActionError(
                f"field {field!r} must be a list of strings, got {type(element).__name__} element"
            )
        elements.append(element)
    if not elements:
        raise InvalidActionError(f"field {field!r} must not be empty")
    return tuple(elements)


def _require_int_list(arguments: Mapping[str, object], field: str) -> tuple[int, ...]:
    if field not in arguments:
        raise InvalidActionError(f"missing required field {field!r}")
    value = arguments[field]
    if not isinstance(value, list):
        raise InvalidActionError(f"field {field!r} must be a list, got {type(value).__name__}")
    raw = cast(list[object], value)
    elements: list[int] = []
    for element in raw:
        if not isinstance(element, int):
            raise InvalidActionError(
                f"field {field!r} must be a list of integers, got {type(element).__name__} element"
            )
        elements.append(element)
    return tuple(elements)


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


def _read_timeout(stream: IO[bytes], deadline: float) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError
            data = os.read(stream.fileno(), 65536)
            if not data:
                tail = decoder.decode(b"", final=True)
                if tail:
                    yield tail
                return
            chunk = decoder.decode(data)
            if chunk:
                yield chunk
    finally:
        selector.close()


@dataclass(frozen=True)
class Shell:
    command: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        command = require(arguments, "command", str)
        return cls(command=command)

    def run(
        self, timeout: float = 30, on_chunk: Callable[[str], None] | None = None
    ) -> tuple[int, str]:
        process = subprocess.Popen(
            self.command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        chunks: list[str] = []
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        with process:
            try:
                for chunk in _read_timeout(process.stdout, deadline):
                    chunks.append(chunk)
                    if on_chunk is not None:
                        on_chunk(chunk)
                code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (TimeoutError, subprocess.TimeoutExpired) as error:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise ToolError(f"command timed out after {timeout}s: {self.command}") from error
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise
        return code, "".join(chunks)

    def execute(self, timeout: float = 30) -> str:
        code, output = self.run(timeout)
        if code != 0:
            return f"exit code {code}\n{output}"
        return output


@dataclass(frozen=True)
class Answer:
    content: str
    citations: tuple[int, ...]
    verify: str | None = None

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        content = require(arguments, "content", str)
        citations = _require_int_list(arguments, "citations")
        verify = None if arguments.get("verify") is None else require(arguments, "verify", str)
        if verify is not None and not verify.strip():
            raise InvalidActionError("field 'verify' must not be empty")
        return cls(content=content, citations=citations, verify=verify)


@dataclass(frozen=True)
class SetPlan:
    steps: tuple[str, ...]

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        steps = _require_str_list(arguments, "steps")
        return cls(steps=steps)


@dataclass(frozen=True)
class CompleteStep:
    index: int

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        index = require(arguments, "index", int)
        return cls(index=index)


FieldType = Literal["string", "number", "boolean"]

FIELD_TYPES: tuple[FieldType, ...] = ("string", "number", "boolean")


def _matches(value: object, type_name: FieldType) -> bool:
    match type_name:
        case "string":
            return isinstance(value, str)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)


def _require_fields(arguments: Mapping[str, object]) -> Mapping[str, FieldType] | None:
    value = arguments.get("fields")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidActionError(f"field 'fields' must be an object, got {type(value).__name__}")
    raw = cast(dict[object, object], value)
    fields: dict[str, FieldType] = {}
    for name, type_name in raw.items():
        if not isinstance(name, str):
            raise InvalidActionError("field 'fields' must have string keys")
        if type_name not in FIELD_TYPES:
            raise InvalidActionError(
                f"field 'fields' has unknown type {type_name!r} for {name!r}; "
                f"expected one of {list(FIELD_TYPES)}"
            )
        fields[name] = type_name
    if not fields:
        raise InvalidActionError("field 'fields' must not be empty")
    return fields


@dataclass(frozen=True)
class ResultShape:
    fields: Mapping[str, FieldType]

    def prompt(self) -> str:
        shape = ", ".join(f'"{name}": <{type_name}>' for name, type_name in self.fields.items())
        return (
            "\n\nAnswer with content that is exactly one JSON object of this shape, "
            f"and nothing else: {{{shape}}}"
        )

    def check(self, content: str) -> str | None:
        try:
            parsed: object = json.loads(content)
        except ValueError:
            return "answer content must be a JSON object, but it did not parse as JSON"
        if not isinstance(parsed, dict):
            return f"answer content must be a JSON object, got {type(parsed).__name__}"
        record = cast(dict[str, object], parsed)
        missing = sorted(set(self.fields) - set(record))
        if missing:
            return f"answer is missing required field(s): {missing}"
        extra = sorted(set(record) - set(self.fields))
        if extra:
            return f"answer has unexpected field(s): {extra}"
        for name, type_name in self.fields.items():
            if not _matches(record[name], type_name):
                return f"field {name!r} must be a {type_name}, got {type(record[name]).__name__}"
        return None


@dataclass(frozen=True)
class Delegate:
    question: str
    shape: ResultShape | None = None

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        question = require(arguments, "question", str)
        fields = _require_fields(arguments)
        return cls(question=question, shape=None if fields is None else ResultShape(fields))


_TOOL_SPECS = {
    "read_file": ToolSpec(
        name="read_file",
        description="Read the contents of a file at the given path.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    "write_file": ToolSpec(
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
    ),
    "note": ToolSpec(
        name="note",
        description=(
            "Record an important finding, conclusion, or decision as a fact. "
            "Older messages fall out of your context, but facts are kept: "
            "noted findings stay in the fact index and can be recovered with "
            "read_fact or found again with search_facts."
        ),
        parameters={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    ),
    "read_fact": ToolSpec(
        name="read_fact",
        description=(
            "Recover the full content of a fact that was truncated to a handle, "
            "given the fact id shown in the handle."
        ),
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    ),
    "set_plan": ToolSpec(
        name="set_plan",
        description=(
            "Replace the current plan with an ordered checklist of steps. "
            "The runtime remembers it and shows it to you every turn."
        ),
        parameters={
            "type": "object",
            "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
            "required": ["steps"],
        },
    ),
    "complete_step": ToolSpec(
        name="complete_step",
        description="Mark the plan step at the given zero-based index as done.",
        parameters={
            "type": "object",
            "properties": {"index": {"type": "integer"}},
            "required": ["index"],
        },
    ),
    "load_table": ToolSpec(
        name="load_table",
        description=(
            "Load a CSV file into a scratch SQL database table without reading it into context, "
            "and return the table's schema and row count. "
            "Prefer this over shell pipelines for anything tabular or numeric: "
            "load the file, then compute with the sql tool."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "table": {"type": "string"},
            },
            "required": ["path", "table"],
        },
    ),
    "sql": ToolSpec(
        name="sql",
        description=(
            "Run one SQL statement against the scratch database and return the rows. "
            "Prefer this over shell pipelines for filtering, joins, aggregation, and arithmetic: "
            "one SELECT with GROUP BY is computed exactly, where awk and sort are guesswork. "
            "CREATE and INSERT work too, so you can stage intermediate results. "
            "This is a tool, not a shell command: there is no 'sql' executable, "
            "so never put it in a shell command or in answer's verify."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
}


def _action_tool(action_type: type[ReadFile | WriteFile], name: str) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        return action_type.from_arguments(arguments).execute()

    return Tool(spec=_TOOL_SPECS[name], execute=execute)


def note_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        content = require(arguments, "content", str)
        if not content.strip():
            raise InvalidActionError("field 'content' must not be empty")
        return content

    return Tool(spec=_TOOL_SPECS["note"], execute=execute)


def fact_recall_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        fact_id = require(arguments, "id", int)
        for fact in facts(session):
            if fact.id == fact_id:
                return fact.content
        raise ToolError(f"no fact with id {fact_id}")

    return Tool(spec=_TOOL_SPECS["read_fact"], execute=execute)


def set_plan_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        action = SetPlan.from_arguments(arguments)
        session.append(PlanSet(steps=action.steps))
        current = plan(session)
        assert current is not None
        return f"plan set:\n{render_plan(current)}"

    return Tool(spec=_TOOL_SPECS["set_plan"], execute=execute)


def complete_step_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        action = CompleteStep.from_arguments(arguments)
        current = plan(session)
        if current is None:
            raise ToolError("no plan set; call set_plan first")
        if not 0 <= action.index < len(current.steps):
            raise ToolError(
                f"no plan step at index {action.index}; the plan has {len(current.steps)} step(s)"
            )
        if current.steps[action.index].done:
            raise ToolError(f"plan step {action.index} is already done")
        session.append(PlanStepCompleted(index=action.index))
        updated = plan(session)
        assert updated is not None
        return f"step {action.index} done:\n{render_plan(updated)}"

    return Tool(spec=_TOOL_SPECS["complete_step"], execute=execute)


def load_table_tool(scratch: Scratch) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        table = require(arguments, "table", str)
        return load_table(scratch, path, table)

    return Tool(spec=_TOOL_SPECS["load_table"], execute=execute)


def sql_tool(scratch: Scratch) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        statement = require(arguments, "query", str)
        return query(scratch, statement)

    return Tool(spec=_TOOL_SPECS["sql"], execute=execute)


def register_actions(registry: ToolRegistry, session: Session, depth: int = 0) -> None:
    registry.register(_action_tool(ReadFile, "read_file"))
    registry.register(_action_tool(WriteFile, "write_file"))
    scratch = Scratch(session, in_memory=depth > 0)
    registry.register(load_table_tool(scratch))
    registry.register(sql_tool(scratch))
    registry.register(note_tool())
    registry.register(fact_recall_tool(session))
    registry.register(set_plan_tool(session))
    registry.register(complete_step_tool(session))
