from dataclasses import dataclass

DECISION_GRACE = 3
MAX_CROSSROADS = 2

DECISION_NUDGE = (
    "decision required — {cause}. either set_plan to hand the remaining work to fresh "
    "agents, or finish with answer. say which one and why, then do it."
)

NARRATION_PRESSURE = "you wrote text but took no action, and you have no plan running"

CROSSROADS_ACTIONS = ("set_plan", "answer", "note")


@dataclass(frozen=True)
class Quiet:
    pass


@dataclass(frozen=True)
class Demanded:
    cause: str
    at: int


@dataclass(frozen=True)
class Crossroads:
    cause: str
    generations: int


DecisionState = Quiet | Demanded | Crossroads


@dataclass(frozen=True)
class Observations:
    undecided: bool
    iterations: int
    pressure: str | None
    narration: str | None


@dataclass(frozen=True)
class Ask:
    nudge: str


@dataclass(frozen=True)
class EndDegraded:
    narration: str | None


Command = Ask | EndDegraded


def advance(state: DecisionState, seen: Observations) -> tuple[DecisionState, Command | None]:
    if not seen.undecided:
        return Quiet(), None
    match state:
        case Quiet():
            if seen.pressure is None:
                return state, None
            arose = seen.iterations - 1 if seen.pressure == NARRATION_PRESSURE else seen.iterations
            return Demanded(seen.pressure, arose), Ask(DECISION_NUDGE.format(cause=seen.pressure))
        case Demanded(cause=cause, at=at):
            if seen.iterations - at > DECISION_GRACE:
                return Crossroads(cause, 1), None
            return state, None
        case Crossroads(cause=cause, generations=generations):
            if generations >= MAX_CROSSROADS:
                return Quiet(), EndDegraded(seen.narration)
            return Crossroads(cause, generations + 1), None
