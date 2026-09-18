from dataclasses import replace

from pico.core.context import transcript_fullness
from pico.core.events import AnswerSettled, ToolCallStarted
from pico.core.ledger import plan
from pico.core.loop.decision import (
    CROSSROADS_ACTIONS,
    DECISION_NUDGE,
    MAX_CROSSROADS,
    NARRATION_PRESSURE,
    Ask,
    Crossroads,
    DecisionState,
    EndDegraded,
    IterationView,
    advance,
)
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.signals import Nudge, Restrict
from pico.core.loop.state import Answered, LastWords, Running, RunState, WindingDown
from pico.core.stuckness import assess
from pico.session import Session

BUDGET_WIND_DOWN_FRACTION = 0.8
CONTEXT_PRESSURE_FRACTION = 0.8

LAST_WORDS_NUDGE = (
    "this run is ending now — {cause}. this is your final generation and answer is the "
    "only tool you have left. answer with what you have found so far, citing the facts "
    "that support it, and say plainly what is still unresolved."
)
CONTEXT_PRESSURE_CAUSE = (
    "your context is nearly full, so the earliest exchanges are falling out of it"
)

LAST_WORDS_ACTIONS = ("answer",)


def stuckness_step(runner: LoopRunner) -> StepOutcome:
    if not isinstance(runner.state, Running):
        return "continue"
    result = assess(runner.session)
    if result.stuck:
        runner.state = WindingDown(f"you are stuck: {result.reason}")
    elif result.nudge is not None:
        runner.emit(Nudge(result.nudge))
    return "continue"


def budget_remaining(
    iterations: int, max_steps: int | None, depth: int, running: bool
) -> int | None:
    if max_steps is None or depth > 0 or not running:
        return None
    if iterations < int(max_steps * BUDGET_WIND_DOWN_FRACTION):
        return None
    return max_steps - iterations


def undecided(session: Session) -> bool:
    current = plan(session)
    return current is None or all(step.done for step in current.steps)


def pressure(fullness: float, remaining: int | None) -> str | None:
    if fullness >= CONTEXT_PRESSURE_FRACTION:
        return CONTEXT_PRESSURE_CAUSE
    if remaining is not None:
        return f"only {remaining} generations remain of your budget"
    return None


def observe(runner: LoopRunner) -> IterationView:
    running = isinstance(runner.state, Running)
    narrated = runner.generation.narration_pressure
    runner.generation.narration_pressure = False
    remaining = budget_remaining(runner.iterations, runner.config.max_steps, runner.depth, running)
    fullness = transcript_fullness(
        runner.session.messages(), runner.context_size, runner.generation.chars_per_token
    )
    return IterationView(
        iterations=runner.iterations,
        remaining=remaining,
        fullness=fullness,
        undecided=running and undecided(runner.session),
        narration=runner.generation.last_narration,
        pressure=NARRATION_PRESSURE if narrated else pressure(fullness, remaining),
    )


def snapshot_step(runner: LoopRunner) -> StepOutcome:
    runner.view = observe(runner)
    return "continue"


def budget_step(runner: LoopRunner) -> StepOutcome:
    remaining = runner.view.remaining
    if remaining is None:
        return "continue"
    runner.emit(
        Nudge(
            f"the generation budget is nearly spent — {remaining} generations remain. "
            "stop exploring, complete or prune the plan, and finish with answer using "
            "the facts you have gathered"
        )
    )
    return "continue"


def decision_step(runner: LoopRunner) -> StepOutcome:
    runner.decision, command = advance(runner.decision, runner.view)
    match command:
        case Ask(nudge=nudge):
            runner.emit(Nudge(nudge))
        case EndDegraded(narration=narration):
            if narration is None:
                runner.state = WindingDown(
                    f"{MAX_CROSSROADS} decision points passed with neither a plan nor an answer"
                )
                return "continue"
            runner.state = Answered(narration)
            pane_id = runner.new_id()
            runner.bus.publish(ToolCallStarted(id=pane_id, name="answer", arguments={}))
            runner.bus.publish(
                AnswerSettled(
                    id=pane_id,
                    content=narration,
                    accepted=True,
                    reason=None,
                    verify=None,
                    complete=False,
                )
            )
            return "done"
        case None:
            pass
    return "continue"


def restriction(state: RunState, decision: DecisionState, nudge: str | None) -> Restrict | None:
    if isinstance(state, LastWords):
        return Restrict(
            allowed=LAST_WORDS_ACTIONS,
            text=LAST_WORDS_NUDGE.format(cause=state.cause),
            rejection="answer is the only tool left",
        )
    if not isinstance(decision, Crossroads):
        return None
    crossroads = Restrict(
        allowed=CROSSROADS_ACTIONS,
        text=DECISION_NUDGE.format(cause=decision.cause),
        rejection=(
            f"a decision is required first; the tools you have are {', '.join(CROSSROADS_ACTIONS)}"
        ),
    )
    if nudge is not None:
        return replace(crossroads, text=nudge)
    return crossroads
