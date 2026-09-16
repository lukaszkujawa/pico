import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self, cast

from pico.core.errors import ToolError
from pico.core.ledger import facts
from pico.core.tools import Tool, ToolRegistry
from pico.llm.types import ToolSpec
from pico.session import Session


class InvalidActionError(ValueError):
    pass


def _require[T](arguments: Mapping[str, object], field: str, expected: type[T]) -> T:
    if field not in arguments:
        raise InvalidActionError(f"missing required field {field!r}")
    value = arguments[field]
    if not isinstance(value, expected):
        raise InvalidActionError(
            f"field {field!r} must be a {expected.__name__}, got {type(value).__name__}"
        )
    return value


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
        path = _require(arguments, "path", str)
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
        path = _require(arguments, "path", str)
        content = _require(arguments, "content", str)
        return cls(path=path, content=content)

    def execute(self) -> str:
        try:
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(self.content)
        except OSError as error:
            raise ToolError(f"could not write {self.path}: {error}") from error
        return f"wrote {len(self.content)} bytes to {self.path}"


@dataclass(frozen=True)
class Shell:
    command: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        command = _require(arguments, "command", str)
        return cls(command=command)

    def execute(self, timeout: float = 30) -> str:
        try:
            result = subprocess.run(
                self.command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ToolError(f"command timed out after {timeout}s: {self.command}") from error
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0:
            return f"exit code {result.returncode}\n{output}"
        return output


@dataclass(frozen=True)
class Answer:
    content: str
    citations: tuple[int, ...]

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        content = _require(arguments, "content", str)
        citations = _require_int_list(arguments, "citations")
        return cls(content=content, citations=citations)


@dataclass(frozen=True)
class Delegate:
    question: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        question = _require(arguments, "question", str)
        return cls(question=question)


Action = ReadFile | WriteFile | Shell | Answer | Delegate

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
    "shell": ToolSpec(
        name="shell",
        description="Run a shell command and return its combined stdout and stderr.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    ),
    "answer": ToolSpec(
        name="answer",
        description="Give the final answer to the user and end the run.",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["content", "citations"],
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
    "delegate": ToolSpec(
        name="delegate",
        description=(
            "Spawn a read-only sub-agent to answer a single scoped question and "
            "return its evidence-checked answer."
        ),
        parameters={
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    ),
}


def _action_tool(action_type: type[ReadFile | WriteFile | Shell], name: str) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        try:
            action = action_type.from_arguments(arguments)
        except InvalidActionError as error:
            return str(error)
        return action.execute()

    return Tool(spec=_TOOL_SPECS[name], execute=execute)


def _answer_tool() -> Tool:
    return Tool(spec=_TOOL_SPECS["answer"], execute=lambda _: "")


def fact_recall_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        try:
            fact_id = _require(arguments, "id", int)
        except InvalidActionError as error:
            return str(error)
        for fact in facts(session):
            if fact.id == fact_id:
                return fact.content
        raise ToolError(f"no fact with id {fact_id}")

    return Tool(spec=_TOOL_SPECS["read_fact"], execute=execute)


def register_actions(registry: ToolRegistry, session: Session) -> None:
    registry.register(_action_tool(ReadFile, "read_file"))
    registry.register(_action_tool(WriteFile, "write_file"))
    registry.register(_action_tool(Shell, "shell"))
    registry.register(fact_recall_tool(session))
    registry.register(_answer_tool())
    registry.register(Tool(spec=_TOOL_SPECS["delegate"], execute=lambda _: ""))


def register_delegate_actions(registry: ToolRegistry, session: Session) -> None:
    registry.register(_action_tool(ReadFile, "read_file"))
    registry.register(fact_recall_tool(session))
    registry.register(_answer_tool())
