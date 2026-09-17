from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from pico.core.actions.arguments import require, require_fields
from pico.core.actions.context import ActionContext, ActionResult, RunnerAction
from pico.core.actions.shape import ResultShape
from pico.llm.types import ToolSpec

MAX_DELEGATE_DEPTH = 3

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


@dataclass(frozen=True)
class Delegate:
    question: str
    shape: ResultShape | None = None

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        question = require(arguments, "question", str)
        fields = require_fields(arguments)
        return cls(question=question, shape=None if fields is None else ResultShape(fields))


def run_delegate(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    result = context.spawn(Delegate.from_arguments(arguments))
    if context.cancel.is_set():
        return None
    return result


DELEGATE_ACTION = RunnerAction(spec=DELEGATE_SPEC, execute=run_delegate)
