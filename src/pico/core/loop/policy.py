from dataclasses import replace

from pico.core.context import RECENT_UNITS, transcript_units
from pico.core.events import AnswerSettled, ToolCallStarted
from pico.core.ledger import plan
from pico.core.loop.decision import (
    CROSSROADS_ACTIONS,
    DECISION_NUDGE,
    MAX_CROSSROADS,
    NARRATION_PRESSURE,
    Ask,
    Crossroads,
    EndDegraded,
    Observations,
    advance,
)
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.signals import Nudge, Restrict
from pico.core.loop.state import Answered, LastWords, Running, WindingDown
from pico.core.stuckness import assess

BUDGET_WIND_DOWN_FRACTION = 0.8

LAST_WORDS_NUDGE = (
    "this run is ending now — {cause}. this is your final generation and answer is the "
    "only tool you have left. answer with what you have found so far, citing the facts "
    "that support it, and say plainly what is still unresolved."
)
CONTEXT_PRESSURE_CAUSE = (
    f"your context has passed {RECENT_UNITS} exchanges, so the earliest ones are now "
    "falling out of it"
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


def budget_nearly_spent(runner: LoopRunner) -> bool:
    max_steps = runner.config.max_steps
    if max_steps is None or runner.depth > 0 or not isinstance(runner.state, Running):
        return False
    return runner.iterations >= int(max_steps * BUDGET_WIND_DOWN_FRACTION)


def budget_step(runner: LoopRunner) -> StepOutcome:
    if not budget_nearly_spent(runner):
        return "continue"
    assert runner.config.max_steps is not None
    remaining = runner.config.max_steps - runner.iterations
    runner.emit(
        Nudge(
            f"the generation budget is nearly spent — {remaining} generations remain. "
            "stop exploring, complete or prune the plan, and finish with answer using "
            "the facts you have gathered"
        )
    )
    return "continue"


def undecided(runner: LoopRunner) -> bool:
    if not isinstance(runner.state, Running):
        return False
    current = plan(runner.session)
    return current is None or all(step.done for step in current.steps)


def pressure(runner: LoopRunner) -> str | None:
    if transcript_units(runner.session.messages()) > RECENT_UNITS:
        return CONTEXT_PRESSURE_CAUSE
    if budget_nearly_spent(runner):
        assert runner.config.max_steps is not None
        remaining = runner.config.max_steps - runner.iterations
        return f"only {remaining} generations remain of your budget"
    return None


def observe(runner: LoopRunner) -> Observations:
    narrated = runner.generation.narration_pressure
    runner.generation.narration_pressure = False
    return Observations(
        undecided=undecided(runner),
        iterations=runner.iterations,
        pressure=NARRATION_PRESSURE if narrated else pressure(runner),
        narration=runner.generation.last_narration,
    )


def decision_step(runner: LoopRunner) -> StepOutcome:
    runner.decision, command = advance(runner.decision, observe(runner))
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


def restriction(runner: LoopRunner, nudge: str | None) -> Restrict | None:
    if isinstance(runner.state, LastWords):
        return Restrict(
            allowed=LAST_WORDS_ACTIONS,
            text=LAST_WORDS_NUDGE.format(cause=runner.state.cause),
            rejection="answer is the only tool left",
        )
    if not isinstance(runner.decision, Crossroads):
        return None
    crossroads = Restrict(
        allowed=CROSSROADS_ACTIONS,
        text=DECISION_NUDGE.format(cause=runner.decision.cause),
        rejection=(
            f"a decision is required first; the tools you have are {', '.join(CROSSROADS_ACTIONS)}"
        ),
    )
    if nudge is not None:
        return replace(crossroads, text=nudge)
    return crossroads
