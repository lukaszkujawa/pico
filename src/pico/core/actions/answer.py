from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from pico.core.actions.arguments import InvalidActionError, require, require_int_list
from pico.core.actions.context import ActionContext, ActionResult, AnswerOutcome, RunnerAction
from pico.core.actions.shell import Shell
from pico.core.ledger import Fact, fact_index, facts
from pico.llm.types import ToolSpec

INLINE_CITATION_INDEX_MAX = 20

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

_PINNING = (
    "Keep your answer's content, fix only the citations, and drop any claim no fact supports."
)
_SEARCH_ROUTE = (
    "Find the ids with search_facts: describe the claim in plain words, "
    'e.g. search_facts("reddit request blocked by bot detection").'
)


@dataclass(frozen=True)
class Answer:
    content: str
    citations: tuple[int, ...]
    verify: str | None = None

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        content = require(arguments, "content", str)
        citations = require_int_list(arguments, "citations")
        verify = None if arguments.get("verify") is None else require(arguments, "verify", str)
        if verify is not None and not verify.strip():
            raise InvalidActionError("field 'verify' must not be empty")
        return cls(content=content, citations=citations, verify=verify)


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


def _citation_rejection(answer: Answer, problem: str, known: list[Fact]) -> AnswerOutcome:
    if not known:
        route = "No facts exist yet to cite — gather one before answering."
    elif len(known) <= INLINE_CITATION_INDEX_MAX:
        route = f"Cite one of the facts you have:\n{fact_index(known)}"
    else:
        route = _SEARCH_ROUTE
    return _rejected(answer, f"{problem}\n{route}\n{_PINNING}")


def _unknown_citations(answer: Answer, known: list[Fact]) -> str | None:
    ids = {fact.id for fact in known}
    unknown = [citation for citation in answer.citations if citation not in ids]
    if not unknown:
        return None
    cited = ", ".join(f"[{citation}]" for citation in unknown)
    verb = "does" if len(unknown) == 1 else "do"
    return f"unknown fact citation(s): fact {cited} {verb} not exist"


def run_answer(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    known = facts(context.session)
    try:
        answer = Answer.from_arguments(arguments)
    except InvalidActionError as error:
        if "citations" not in str(error):
            raise
        content = arguments.get("content")
        draft = Answer(content=content if isinstance(content, str) else "", citations=())
        return _citation_rejection(draft, str(error), known)
    problem = _unknown_citations(answer, known)
    if problem is not None:
        return _citation_rejection(answer, problem, known)
    problem = None if context.result_shape is None else context.result_shape.check(answer.content)
    if problem is not None:
        return _rejected(answer, problem)
    if answer.verify is None:
        return _accepted(context, answer, answer.content)
    code, output = Shell(command=answer.verify).run()
    if code != 0:
        return _rejected(answer, f"verification failed (exit {code}):\n{output}")
    return _accepted(context, answer, f"{answer.content}\n\nverified: {answer.verify}")


ANSWER_ACTION = RunnerAction(spec=ANSWER_SPEC, execute=run_answer)
