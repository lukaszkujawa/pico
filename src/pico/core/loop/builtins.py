import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    Answer,
    Delegate,
    InvalidActionError,
    ResultShape,
    Shell,
    require,
)
from pico.core.bus import Bus
from pico.core.events import ToolCallResultDelta
from pico.core.ledger import facts
from pico.core.search import search
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.errors import LLMError
from pico.llm.types import ToolSpec
from pico.session import Session


@dataclass(frozen=True)
class AnswerOutcome:
    content: str
    result: str
    is_error: bool
    accepted: bool
    reason: str | None
    verify: str | None


ActionResult = AnswerOutcome | tuple[str, bool] | None

Spawn = Callable[[Delegate], tuple[str, bool]]


@dataclass
class ActionContext:
    session: Session
    llm: LLMClient
    bus: Bus
    pane_id: str
    cancel: threading.Event
    context_size: int
    chars_per_token: float
    result_shape: ResultShape | None
    spawn: Spawn
    final_answer: str | None = field(default=None, init=False)


@dataclass(frozen=True)
class RunnerAction:
    spec: ToolSpec
    execute: Callable[[ActionContext, Mapping[str, object]], ActionResult]


SHELL_SPEC = ToolSpec(
    name="shell",
    description="Run a shell command and return its combined stdout and stderr.",
    parameters={
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
)


def run_shell(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    def on_chunk(chunk: str) -> None:
        context.bus.publish(ToolCallResultDelta(id=context.pane_id, text=chunk))

    code, output = Shell.from_arguments(arguments).run(on_chunk=on_chunk)
    if code != 0:
        return f"exit code {code}\n{output}", True
    return output, False


ANSWER_SPEC = ToolSpec(
    name="answer",
    description=(
        "Give the final answer to the user and end the run. "
        "Whenever the task has a checkable outcome, pass verify: a shell command "
        "that exits 0 exactly when your answer's claim is true. The runtime runs "
        "it before accepting the answer and rejects the answer if it fails."
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
)


def _rejected(answer: Answer, reason: str) -> AnswerOutcome:
    return AnswerOutcome(
        content=answer.content,
        result=f"answer rejected — {reason}",
        is_error=True,
        accepted=False,
        reason=reason,
        verify=answer.verify,
    )


def _accepted(context: ActionContext, answer: Answer, result: str) -> AnswerOutcome:
    context.final_answer = answer.content
    return AnswerOutcome(
        content=answer.content,
        result=result,
        is_error=False,
        accepted=True,
        reason=None,
        verify=answer.verify,
    )


def run_answer(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    answer = Answer.from_arguments(arguments)
    known = {fact.id for fact in facts(context.session)}
    unknown = [citation for citation in answer.citations if citation not in known]
    if unknown:
        raise InvalidActionError(f"unknown fact citation(s): {unknown}")
    problem = None if context.result_shape is None else context.result_shape.check(answer.content)
    if problem is not None:
        return _rejected(answer, problem)
    if answer.verify is None:
        return _accepted(context, answer, answer.content)
    code, output = Shell(command=answer.verify).run()
    if code != 0:
        return _rejected(answer, f"verification failed (exit {code}):\n{output}")
    return _accepted(context, answer, f"{answer.content}\n\nverified: {answer.verify}")


SEARCH_SPEC = ToolSpec(
    name="search_facts",
    description=(
        "Search all recorded facts (tool results and notes) by describing what you "
        "are looking for in plain words. A sub-task reads every recorded fact and "
        "judges relevance against your query, so keywords need not appear "
        "literally. Returns the relevant fact ids, each with a reason; "
        "read_fact(id) recovers any of them in full."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)


def run_search(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    query = require(arguments, "query", str)
    if not query.strip():
        raise InvalidActionError("field 'query' must not be empty")
    try:
        output = search(
            context.llm,
            context.session,
            query,
            context.context_size,
            context.chars_per_token,
            context.cancel,
        )
    except LLMError as error:
        return f"search failed: {error}", True
    return output, False


DELEGATE_SPEC = ToolSpec(
    name="delegate",
    description=(
        "Spawn a sub-agent with its own fresh context to answer a single scoped "
        "question and return its answer. It has the same tools as you: it can "
        "explore with shell, work to its own plan, and delegate further. Use it "
        "to keep large exploration out of your own context. Pass fields to "
        "require a typed result: a mapping of field name to 'string', 'number', "
        "or 'boolean'. The runtime then rejects any answer that is not a JSON "
        "object with exactly those fields, so what comes back is "
        "machine-readable."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "fields": {
                "type": "object",
                "additionalProperties": {
                    "type": "string",
                    "enum": ["string", "number", "boolean"],
                },
            },
        },
        "required": ["question"],
    },
)


def run_delegate_action(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    result = context.spawn(Delegate.from_arguments(arguments))
    if context.cancel.is_set():
        return None
    return result


RUNNER_ACTIONS: dict[str, RunnerAction] = {
    action.spec.name: action
    for action in (
        RunnerAction(spec=SHELL_SPEC, execute=run_shell),
        RunnerAction(spec=ANSWER_SPEC, execute=run_answer),
        RunnerAction(spec=SEARCH_SPEC, execute=run_search),
        RunnerAction(spec=DELEGATE_SPEC, execute=run_delegate_action),
    )
}


def vocabulary(
    tools: ToolRegistry, depth: int, allowed: tuple[str, ...] | None = None
) -> list[ToolSpec]:
    specs = [*tools.specs(), *(action.spec for action in RUNNER_ACTIONS.values())]
    if depth >= MAX_DELEGATE_DEPTH:
        specs = [spec for spec in specs if spec.name != "delegate"]
    if allowed is None:
        return specs
    by_name = {spec.name: spec for spec in specs}
    return [by_name[name] for name in allowed if name in by_name]
