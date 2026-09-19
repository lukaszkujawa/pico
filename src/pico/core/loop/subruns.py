from collections.abc import Mapping

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    Delegate,
    InvalidActionError,
    Outcome,
    ResultShape,
    register_actions,
)
from pico.core.bus import Bus
from pico.core.events import ToolCallStarted
from pico.core.ledger import Plan, plan, render_plan
from pico.core.loop.record import finish_tool_call
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.state import Answered, Failed, Running, StepState, WindingDown
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolCall
from pico.session import PlanStepCompleted, Session, ToolCallRecorded, UserMessageRecorded

MAX_STEP_ATTEMPTS = 2


def run_child(
    runner: LoopRunner,
    suffix: str,
    prompt: str,
    shape: ResultShape | None = None,
) -> LoopRunner:
    child_session = runner.session.child(suffix)
    child_session.append(UserMessageRecorded(content=prompt))
    child_tools = ToolRegistry()
    vision = any(spec.name == "view_image" for spec in runner.tools.specs())
    register_actions(child_tools, child_session, depth=runner.depth + 1, vision=vision)
    child_runner = LoopRunner(
        runner.llm,
        child_tools,
        Bus(),
        child_session,
        runner.context_size,
        runner.config,
        cancel=runner.cancel,
        result_shape=shape,
        depth=runner.depth + 1,
    )
    child_runner.execute()
    return child_runner


def conclude(child: LoopRunner) -> Outcome:
    match child.state:
        case Answered(content, None):
            return Outcome(content)
        case Answered(content, cause):
            return Outcome(f"partial — {cause}:\n{content}")
        case Failed(reason):
            return Outcome(reason, is_error=True)
        case _:
            return Outcome(f"no answer within {child.max_steps} steps", is_error=True)


def spawn_delegate(runner: LoopRunner, delegate: Delegate) -> Outcome:
    if runner.depth >= MAX_DELEGATE_DEPTH:
        raise InvalidActionError("delegate is not available at this depth")
    prompt = "" if delegate.shape is None else delegate.shape.prompt()
    child_runner = run_child(
        runner,
        f"delegate/{runner.session.next_seq()}",
        delegate.question + prompt,
        delegate.shape,
    )
    outcome = conclude(child_runner)
    if outcome.is_error:
        return Outcome(f"delegate failed: {outcome.result}", is_error=True)
    return outcome


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
        "your context, so state the findings the later steps need, not just that you are done. "
        "Before answering, note each finding the next step will build on with a check that "
        "proves it."
    )
    return "\n\n".join(sections)


def _first_unfinished(current: Plan | None) -> int | None:
    if current is None:
        return None
    return next((index for index, step in enumerate(current.steps) if not step.done), None)


def spawn_step(runner: LoopRunner, current: Plan, index: int) -> Outcome | None:
    child = run_child(
        runner,
        f"step/{runner.session.next_seq()}",
        compose_handoff(runner.session, current, index),
    )
    if runner.cancel.is_set():
        return None
    outcome = conclude(child)
    if outcome.is_error:
        text = current.steps[index].text
        return Outcome(f'step {index} failed — {outcome.result}\nstep was: "{text}"', is_error=True)
    return outcome


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
    outcome = spawn_step(runner, current, index)
    if outcome is None:
        return "cancelled"
    call = ToolCall(id=pane_id, name="step", arguments=arguments)
    finish_tool_call(runner.session, runner.bus, pane_id, call, outcome)
    if not outcome.is_error:
        runner.session.append(PlanStepCompleted(index=index))
    return "continue"
