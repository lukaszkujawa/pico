from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from pico.core.actions.arguments import require, require_str_list
from pico.core.errors import ToolError
from pico.core.ledger import plan, render_plan
from pico.core.tools import Tool
from pico.llm.types import ToolSpec
from pico.session import PlanSet, PlanStepCompleted, Session

SET_PLAN_SPEC = ToolSpec(
    name="set_plan",
    description=(
        "Break work too big for one context into steps, each run with a fresh context — "
        "so the whole task gets more thinking than your own context could hold. "
        "Replace the current plan with an ordered checklist of steps. "
        "The runtime then runs each unfinished step for you: a fresh agent executes it "
        "and its answer settles the step, so you see results rather than doing the work. "
        "That agent sees only the task, the plan, earlier steps' results, and its own "
        "step text — so write every step to stand alone: name the paths, commands, and "
        'targets it needs, and never refer to "the files above" or anything only you '
        "can see."
    ),
    parameters={
        "type": "object",
        "properties": {"steps": {"type": "array", "items": {"type": "string"}}},
        "required": ["steps"],
    },
)


@dataclass(frozen=True)
class SetPlan:
    steps: tuple[str, ...]

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        steps = require_str_list(arguments, "steps")
        return cls(steps=steps)


def set_plan_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        action = SetPlan.from_arguments(arguments)
        session.append(PlanSet(steps=action.steps))
        current = plan(session)
        assert current is not None
        return f"plan set:\n{render_plan(current)}"

    return Tool(spec=SET_PLAN_SPEC, execute=execute)


COMPLETE_STEP_SPEC = ToolSpec(
    name="complete_step",
    description="Mark the plan step at the given zero-based index as done.",
    parameters={
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
    },
)


@dataclass(frozen=True)
class CompleteStep:
    index: int

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, object]) -> Self:
        index = require(arguments, "index", int)
        return cls(index=index)


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

    return Tool(spec=COMPLETE_STEP_SPEC, execute=execute)
