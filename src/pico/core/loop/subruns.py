from collections.abc import Mapping

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    Delegate,
    InvalidActionError,
    ResultShape,
    register_actions,
)
from pico.core.bus import Bus
from pico.core.events import ToolCallFinished, ToolCallStarted
from pico.core.ledger import Plan, facts, plan, render_plan
from pico.core.loop.runner import LoopConfig, LoopRunner, StepOutcome
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolCall
from pico.session import PlanStepCompleted, Session, ToolCallRecorded, UserMessageRecorded

MAX_DELEGATE_STEPS = 10
MAX_STEP_STEPS = 30
MAX_STEP_ATTEMPTS = 2


def run_child(
    runner: LoopRunner,
    suffix: str,
    prompt: str,
    max_steps: int,
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
        LoopConfig(steps=runner.config.steps, max_steps=max_steps),
        cancel=runner.cancel,
        result_shape=shape,
        depth=runner.depth + 1,
    )
    child_runner.execute()
    return child_runner


def run_delegate(runner: LoopRunner, delegate: Delegate) -> tuple[str, bool]:
    if runner.depth >= MAX_DELEGATE_DEPTH:
        raise InvalidActionError("delegate is not available at this depth")
    prompt = "" if delegate.shape is None else delegate.shape.prompt()
    child_runner = run_child(
        runner,
        f"delegate/{runner.session.next_seq()}",
        delegate.question + prompt,
        MAX_DELEGATE_STEPS,
        delegate.shape,
    )
    if child_runner.final_answer is not None:
        return child_runner.final_answer, False
    if child_runner.error is not None:
        return f"delegate failed: {child_runner.error}", True
    return (
        f"delegate did not answer question within {MAX_DELEGATE_STEPS} steps: "
        f"{delegate.question!r}",
        True,
    )


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


def _step_result(child: LoopRunner, index: int, text: str) -> tuple[str, bool]:
    if child.final_answer is not None:
        if not child.last_words:
            return child.final_answer, False
        return f"partial — {child.dying_of}:\n{child.final_answer}", False
    reason = child.error if child.error is not None else f"no answer within {MAX_STEP_STEPS} steps"
    return f'step {index} failed — {reason}\nstep was: "{text}"', True


def step_orchestration_step(runner: LoopRunner) -> StepOutcome:
    if runner.depth >= MAX_DELEGATE_DEPTH or runner.dying_of is not None:
        return "continue"
    current = plan(runner.session)
    index = _first_unfinished(current)
    if current is None or index is None:
        runner.steps.attempts = {}
        return "continue"
    signature = (tuple(step.text for step in current.steps), index)
    attempts = runner.steps.attempts.get(signature, 0)
    if attempts >= MAX_STEP_ATTEMPTS:
        runner.dying_of = f'step {index} failed twice: "{current.steps[index].text}"'
        return "continue"
    runner.steps.attempts[signature] = attempts + 1

    text = current.steps[index].text
    pane_id = runner.new_id()
    arguments: Mapping[str, object] = {"step": text}
    runner.bus.publish(ToolCallStarted(id=pane_id, name="step", arguments=arguments))
    child = run_child(
        runner,
        f"step/{runner.session.next_seq()}",
        compose_handoff(runner.session, current, index),
        MAX_STEP_STEPS,
    )
    if runner.cancel.is_set():
        return "cancelled"
    result, is_error = _step_result(child, index, text)
    runner.session.append(
        ToolCallRecorded(name="step", arguments=arguments, result=result, is_error=is_error)
    )
    if not is_error:
        runner.session.append(PlanStepCompleted(index=index))
    fact_id = facts(runner.session)[-1].id if not is_error else None
    runner.bus.publish(
        ToolCallFinished(
            id=pane_id,
            tool_call=ToolCall(id=pane_id, name="step", arguments=arguments),
            result=result,
            is_error=is_error,
            fact_id=fact_id,
        )
    )
    return "continue"
