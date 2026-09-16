import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, Self, cast

from pico.core.errors import ToolError
from pico.core.ledger import facts, plan, render_plan
from pico.core.tools import Tool, ToolRegistry
from pico.llm.types import ToolSpec
from pico.session import PlanSet, PlanStepCompleted, Session


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


def _read_timeout(stream: IO[str], timeout: float) -> Iterator[str]:
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError
            line = stream.readline()
            if not line:
                return
            yield line
    finally:
        selector.close()


@dataclass(frozen=True)
class Shell:
    command: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        command = _require(arguments, "command", str)
        return cls(command=command)

    def run(
        self, timeout: float = 30, on_chunk: Callable[[str], None] | None = None
    ) -> tuple[int, str]:
        process = subprocess.Popen(
            self.command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        chunks: list[str] = []
        assert process.stdout is not None
        try:
            for line in _read_timeout(process.stdout, timeout):
                chunks.append(line)
                if on_chunk is not None:
                    on_chunk(line)
        except TimeoutError as error:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ToolError(f"command timed out after {timeout}s: {self.command}") from error
        code = process.wait()
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
        content = _require(arguments, "content", str)
        citations = _require_int_list(arguments, "citations")
        verify = None if arguments.get("verify") is None else _require(arguments, "verify", str)
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
        index = _require(arguments, "index", int)
        return cls(index=index)


@dataclass(frozen=True)
class Delegate:
    question: str

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        question = _require(arguments, "question", str)
        return cls(question=question)


Action = ReadFile | WriteFile | Shell | Answer | Delegate | SetPlan | CompleteStep

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
        description=(
            "Give the final answer to the user and end the run. "
            "Whenever the task has a checkable outcome, pass verify: a shell command that "
            "exits 0 exactly when your answer's claim is true. The runtime runs it before "
            "accepting the answer and rejects the answer if it fails."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "integer"}},
                "verify": {"type": "string"},
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


def set_plan_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        try:
            action = SetPlan.from_arguments(arguments)
        except InvalidActionError as error:
            return str(error)
        session.append(PlanSet(steps=action.steps))
        current = plan(session)
        assert current is not None
        return f"plan set:\n{render_plan(current)}"

    return Tool(spec=_TOOL_SPECS["set_plan"], execute=execute)


def complete_step_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        try:
            action = CompleteStep.from_arguments(arguments)
        except InvalidActionError as error:
            return str(error)
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


def register_actions(registry: ToolRegistry, session: Session) -> None:
    registry.register(_action_tool(ReadFile, "read_file"))
    registry.register(_action_tool(WriteFile, "write_file"))
    registry.register(_action_tool(Shell, "shell"))
    registry.register(fact_recall_tool(session))
    registry.register(set_plan_tool(session))
    registry.register(complete_step_tool(session))
    registry.register(_answer_tool())
    registry.register(Tool(spec=_TOOL_SPECS["delegate"], execute=lambda _: ""))


def register_delegate_actions(registry: ToolRegistry, session: Session) -> None:
    registry.register(_action_tool(ReadFile, "read_file"))
    registry.register(fact_recall_tool(session))
    registry.register(_answer_tool())
