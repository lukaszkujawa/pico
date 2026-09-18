from collections.abc import Mapping

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    Delegate,
    InvalidActionError,
    ResultShape,
    register_actions,
)
from pico.core.bus import Bus
from pico.core.events import ToolCallStarted
from pico.core.ledger import Plan, plan, render_plan
from pico.core.loop.record import finish_tool_call
from pico.core.loop.runner import LoopConfig, LoopRunner, StepOutcome
from pico.core.loop.state import Answered, Failed, Running, StepState, WindingDown
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolCall
from pico.session import PlanStepCompleted, Session, ToolCallRecorded, UserMessageRecorded

CHILD_BUDGETS = (30, 10)
MAX_STEP_ATTEMPTS = 2


def child_budget(depth: int) -> int:
    assert depth > 0, "depth 0 is the root run, not a child"
    return CHILD_BUDGETS[min(depth, len(CHILD_BUDGETS)) - 1]


def run_child(
    runner: LoopRunner,
    suffix: str,
    prompt: str,
    shape: ResultShape | None = None,
) -> LoopRunner:
    child_session = runner.session.child(suffix)
    child_session.append(UserMessageRecorded(content=prompt))
    child_tools = ToolRegistry()
    register_actions(child_tools, child_session, depth=runner.depth + 1)
    child_runner = LoopRunner(
        runner.llm,
        child_tools,
        Bus(),
        child_session,
        runner.context_size,
        LoopConfig(steps=runner.config.steps, max_steps=child_budget(runner.depth + 1)),
        cancel=runner.cancel,
        result_shape=shape,
        depth=runner.depth + 1,
    )
    child_runner.execute()
    return child_runner


def conclude(child: LoopRunner) -> tuple[str, bool]:
    match child.state:
        case Answered(content, None):
            return content, False
        case Answered(content, cause):
            return f"partial — {cause}:\n{content}", False
        case Failed(reason):
            return reason, True
        case _:
            return f"no answer within {child.config.max_steps} steps", True


def spawn_delegate(runner: LoopRunner, delegate: Delegate) -> tuple[str, bool]:
    if runner.depth >= MAX_DELEGATE_DEPTH:
        raise InvalidActionError("delegate is not available at this depth")
    prompt = "" if delegate.shape is None else delegate.shape.prompt()
    child_runner = run_child(
        runner,
        f"delegate/{runner.session.next_seq()}",
        delegate.question + prompt,
        delegate.shape,
    )
    result, is_error = conclude(child_runner)
    if is_error:
        return f"delegate failed: {result}", True
    return result, False


def root_task(session: Session) -> str:
    return next(
        (event.content for event in session.events() if isinstance(event, UserMessageRecorded)), ""
    )


def completed_step_results(session: Session) -> list[tuple[str, str]]:
    return [
        (str(event.arguments.get("step", "")), event.result)
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step" and not event.is_error
    ]


def compose_handoff(session: Session, current: Plan, index: int) -> str:
    sections = [
        f"You are working on this task:\n{root_task(session)}",
        f"It has been broken into steps:\n{render_plan(current)}",
    ]
    results = completed_step_results(session)
    if results:
        finished = "\n\n".join(f'Result of "{text}":\n{result}' for text, result in results)
        sections.append(f"Earlier steps produced these results:\n\n{finished}")
    sections.append(
        f"Your step is step {index}: {current.steps[index].text}\n"
        "Do only this step, then answer with its result. Your answer is all that survives "
        "your context, so state the findings the later steps need, not just that you are done."
    )
    return "\n\n".join(sections)


def _first_unfinished(current: Plan | None) -> int | None:
    if current is None:
        return None
    return next((index for index, step in enumerate(current.steps) if not step.done), None)


def spawn_step(runner: LoopRunner, current: Plan, index: int) -> tuple[str, bool] | None:
    child = run_child(
        runner,
        f"step/{runner.session.next_seq()}",
        compose_handoff(runner.session, current, index),
    )
    if runner.cancel.is_set():
        return None
    result, is_error = conclude(child)
    if is_error:
        text = current.steps[index].text
        return f'step {index} failed — {result}\nstep was: "{text}"', True
    return result, False


def claim_step_attempt(steps: StepState, current: Plan, index: int) -> bool:
    signature = (tuple(step.text for step in current.steps), index)
    attempts = steps.attempts.get(signature, 0)
    if attempts >= MAX_STEP_ATTEMPTS:
        return False
    steps.attempts[signature] = attempts + 1
    return True


def step_orchestration_step(runner: LoopRunner) -> StepOutcome:
    if runner.depth >= MAX_DELEGATE_DEPTH or not isinstance(runner.state, Running):
        return "continue"
    current = plan(runner.session)
    index = _first_unfinished(current)
    if current is None or index is None:
        runner.steps = StepState()
        return "continue"
    if not claim_step_attempt(runner.steps, current, index):
        runner.state = WindingDown(f'step {index} failed twice: "{current.steps[index].text}"')
        return "continue"
    return _run_and_record_step(runner, current, index)


def _run_and_record_step(runner: LoopRunner, current: Plan, index: int) -> StepOutcome:
    pane_id = runner.new_id()
    arguments: Mapping[str, object] = {"step": current.steps[index].text}
    runner.bus.publish(ToolCallStarted(id=pane_id, name="step", arguments=arguments))
    concluded = spawn_step(runner, current, index)
    if concluded is None:
        return "cancelled"
    result, is_error = concluded
    finish_tool_call(
        runner.session,
        runner.bus,
        pane_id,
        ToolCall(id=pane_id, name="step", arguments=arguments),
        result,
        is_error,
    )
    if not is_error:
        runner.session.append(PlanStepCompleted(index=index))
    return "continue"
