from dataclasses import dataclass, replace

from pico.core.context import RECENT_UNITS, transcript_units
from pico.core.events import AnswerSettled
from pico.core.ledger import plan
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.signals import Nudge, Restrict, Signal
from pico.core.stuckness import assess

BUDGET_WIND_DOWN_FRACTION = 0.8
DECISION_GRACE = 3
MAX_CROSSROADS = 2

LAST_WORDS_NUDGE = (
    "this run is ending now — {cause}. this is your final generation and answer is the "
    "only tool you have left. answer with what you have found so far, citing the facts "
    "that support it, and say plainly what is still unresolved."
)
DECISION_NUDGE = (
    "decision required — {cause}. either set_plan to hand the remaining work to fresh "
    "agents, or finish with answer. say which one and why, then do it."
)
CONTEXT_PRESSURE_CAUSE = (
    f"your context has passed {RECENT_UNITS} exchanges, so the earliest ones are now "
    "falling out of it"
)

CROSSROADS_ACTIONS = ("set_plan", "answer", "note")
LAST_WORDS_ACTIONS = ("answer",)
UNVERIFIED_PREFIX = (
    "[unverified — the run ended without a final answer; this is its last narration, "
    "with no citations and no verification]"
)


def stuckness_step(runner: LoopRunner) -> StepOutcome:
    if runner.dying_of is not None:
        return "continue"
    result = assess(runner.session)
    if result.stuck:
        runner.dying_of = f"you are stuck: {result.reason}"
    elif result.nudge is not None:
        runner.emit(Nudge(result.nudge))
    return "continue"


def winding_down(runner: LoopRunner) -> bool:
    max_steps = runner.config.max_steps
    if max_steps is None or runner.depth > 0 or runner.dying_of is not None:
        return False
    return runner.iterations >= int(max_steps * BUDGET_WIND_DOWN_FRACTION)


def budget_step(runner: LoopRunner) -> StepOutcome:
    if not winding_down(runner):
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
    if runner.dying_of is not None or runner.final_answer is not None:
        return False
    current = plan(runner.session)
    return current is None or all(step.done for step in current.steps)


@dataclass(frozen=True)
class Pressure:
    name: str
    cause: str


NARRATION_PRESSURE = Pressure(
    "narration", "you wrote text but took no action, and you have no plan running"
)


def pressure(runner: LoopRunner) -> Pressure | None:
    if transcript_units(runner.session.messages()) > RECENT_UNITS:
        return Pressure("context", CONTEXT_PRESSURE_CAUSE)
    if winding_down(runner):
        assert runner.config.max_steps is not None
        remaining = runner.config.max_steps - runner.iterations
        return Pressure("budget", f"only {remaining} generations remain of your budget")
    return None


def decision_step(runner: LoopRunner) -> StepOutcome:
    decision = runner.decision
    if decision.crossroads:
        decision.crossroads = False
        decision.crossroads_generations += 1
    if not undecided(runner):
        decision.demanded.clear()
        decision.demanded_at = None
        decision.crossroads_generations = 0
        return "continue"
    if decision.crossroads_generations >= MAX_CROSSROADS:
        return _degraded_ending(runner)
    if decision.demanded_at is not None:
        ignored_for = runner.iterations - decision.demanded_at
        decision.crossroads = decision.crossroads_generations > 0 or ignored_for > DECISION_GRACE
        return "continue"
    found = pressure(runner)
    if found is not None:
        demand(runner, found)
    return "continue"


def record_narration(runner: LoopRunner, text: str) -> None:
    runner.decision.last_narration = text


def demand(runner: LoopRunner, found: Pressure) -> None:
    decision = runner.decision
    if found.name in decision.demanded:
        return
    decision.demanded.add(found.name)
    decision.demanded_at = runner.iterations
    runner.emit(Nudge(DECISION_NUDGE.format(cause=found.cause)))


def _crossroads_signal(runner: LoopRunner) -> Restrict:
    found = pressure(runner)
    cause = CONTEXT_PRESSURE_CAUSE if found is None else found.cause
    return Restrict(
        allowed=CROSSROADS_ACTIONS,
        text=DECISION_NUDGE.format(cause=cause),
        rejection=(
            f"a decision is required first; the tools you have are {', '.join(CROSSROADS_ACTIONS)}"
        ),
    )


def restriction(runner: LoopRunner, signal: Signal | None) -> Restrict | None:
    if runner.dying_of is not None:
        return Restrict(
            allowed=LAST_WORDS_ACTIONS,
            text=LAST_WORDS_NUDGE.format(cause=runner.dying_of),
            rejection="answer is the only tool left",
        )
    if not runner.decision.crossroads:
        return None
    crossroads = _crossroads_signal(runner)
    if isinstance(signal, Nudge):
        return replace(crossroads, text=signal.text)
    return crossroads


def _degraded_ending(runner: LoopRunner) -> StepOutcome:
    narration = runner.decision.last_narration
    if narration is None:
        runner.dying_of = (
            f"{MAX_CROSSROADS} decision points passed with neither a plan nor an answer"
        )
        return "continue"
    runner.final_answer = f"{UNVERIFIED_PREFIX}\n\n{narration}"
    runner.bus.publish(
        AnswerSettled(
            id=runner.new_id(), content=runner.final_answer, accepted=True, reason=None, verify=None
        )
    )
    return "done"
