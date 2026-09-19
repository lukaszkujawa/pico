from dataclasses import dataclass, replace

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
BUDGET_NUDGE = (
    "the generation budget is nearly spent — {remaining} generations remain. "
    "stop exploring, complete or prune the plan, and finish with answer using "
    "the facts you have gathered"
)
BUDGET_PRESSURE = "only {remaining} generations remain of your budget"

LAST_WORDS_ACTIONS = ("answer",)


@dataclass(frozen=True)
class Verdict:
    nudges: tuple[str, ...] = ()
    pressure: str | None = None
    transition: RunState | None = None
    decision: DecisionState | None = None
    degraded: str | None = None


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


def felt_pressure(narrated: bool, fullness: float) -> str | None:
    if narrated:
        return NARRATION_PRESSURE
    return CONTEXT_PRESSURE_CAUSE if fullness >= CONTEXT_PRESSURE_FRACTION else None


def observe(runner: LoopRunner) -> IterationView:
    running = isinstance(runner.state, Running)
    fullness = transcript_fullness(
        runner.session.messages(), runner.context_size, runner.generation.chars_per_token
    )
    return IterationView(
        iterations=runner.iterations,
        remaining=budget_remaining(
            runner.iterations, runner.config.max_steps, runner.depth, running
        ),
        fullness=fullness,
        undecided=running and undecided(runner.session),
        narration=runner.generation.last_narration,
        pressure=felt_pressure(runner.generation.narration_pressure, fullness),
    )


def stuckness_rule(runner: LoopRunner) -> Verdict:
    if not isinstance(runner.state, Running):
        return Verdict()
    result = assess(runner.session)
    if result.stuck:
        return Verdict(transition=WindingDown(f"you are stuck: {result.reason}"))
    return Verdict() if result.nudge is None else Verdict(nudges=(result.nudge,))


def budget_rule(runner: LoopRunner) -> Verdict:
    remaining = runner.view.remaining
    if remaining is None:
        return Verdict()
    nudge = BUDGET_NUDGE.format(remaining=remaining)
    return Verdict(nudges=(nudge,), pressure=BUDGET_PRESSURE.format(remaining=remaining))


def decision_rule(runner: LoopRunner) -> Verdict:
    decision, command = advance(runner.decision, runner.view)
    match command:
        case Ask(nudge=nudge):
            return Verdict(nudges=(nudge,), decision=decision)
        case EndDegraded(narration=str(narration)):
            return Verdict(transition=Answered(narration), decision=decision, degraded=narration)
        case EndDegraded():
            cause = f"{MAX_CROSSROADS} decision points passed with neither a plan nor an answer"
            return Verdict(transition=WindingDown(cause), decision=decision)
        case None:
            return Verdict(decision=decision)


def _publish_degraded_answer(runner: LoopRunner, narration: str) -> None:
    pane_id = runner.new_id()
    runner.bus.publish(ToolCallStarted(id=pane_id, name="answer", arguments={}))
    runner.bus.publish(
        AnswerSettled(
            id=pane_id, content=narration, accepted=True, reason=None, verify=None, complete=False
        )
    )


def apply_verdict(runner: LoopRunner, verdict: Verdict) -> StepOutcome:
    for nudge in verdict.nudges:
        runner.emit(Nudge(nudge))
    if verdict.pressure is not None and runner.view.pressure is None:
        runner.view = replace(runner.view, pressure=verdict.pressure)
    if verdict.transition is not None:
        runner.state = verdict.transition
    if verdict.decision is not None:
        runner.decision = verdict.decision
    if verdict.degraded is None:
        return "continue"
    _publish_degraded_answer(runner, verdict.degraded)
    return "done"


def policy_step(runner: LoopRunner) -> StepOutcome:
    apply_verdict(runner, stuckness_rule(runner))
    runner.view = observe(runner)
    apply_verdict(runner, budget_rule(runner))
    return apply_verdict(runner, decision_rule(runner))


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
