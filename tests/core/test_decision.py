import pytest

from pico.core.loop.decision import (
    DECISION_GRACE,
    DECISION_NUDGE,
    MAX_CROSSROADS,
    NARRATION_PRESSURE,
    Ask,
    Command,
    Crossroads,
    DecisionState,
    Demanded,
    EndDegraded,
    Observations,
    Quiet,
    advance,
)

CAUSE = "your context has passed the structural bound"


def seen(
    undecided: bool = True,
    iterations: int = 1,
    pressure: str | None = None,
    narration: str | None = None,
) -> Observations:
    return Observations(
        undecided=undecided, iterations=iterations, pressure=pressure, narration=narration
    )


TRANSITIONS: list[tuple[DecisionState, Observations, DecisionState, Command | None]] = [
    (Quiet(), seen(), Quiet(), None),
    (Quiet(), seen(undecided=False, pressure=CAUSE), Quiet(), None),
    (
        Quiet(),
        seen(iterations=4, pressure=CAUSE),
        Demanded(CAUSE, 4),
        Ask(DECISION_NUDGE.format(cause=CAUSE)),
    ),
    (
        Quiet(),
        seen(iterations=4, pressure=NARRATION_PRESSURE),
        Demanded(NARRATION_PRESSURE, 3),
        Ask(DECISION_NUDGE.format(cause=NARRATION_PRESSURE)),
    ),
    (
        Demanded(CAUSE, 1),
        seen(iterations=1 + DECISION_GRACE, pressure=CAUSE),
        Demanded(CAUSE, 1),
        None,
    ),
    (
        Demanded(CAUSE, 1),
        seen(iterations=2, pressure=NARRATION_PRESSURE),
        Demanded(CAUSE, 1),
        None,
    ),
    (Demanded(CAUSE, 1), seen(iterations=2 + DECISION_GRACE), Crossroads(CAUSE, 1), None),
    (Demanded(CAUSE, 1), seen(undecided=False, iterations=2), Quiet(), None),
    (Crossroads(CAUSE, 1), seen(iterations=6), Crossroads(CAUSE, 2), None),
    (Crossroads(CAUSE, 1), seen(undecided=False, iterations=6), Quiet(), None),
    (
        Crossroads(CAUSE, MAX_CROSSROADS),
        seen(iterations=7, narration="musing 5"),
        Quiet(),
        EndDegraded("musing 5"),
    ),
    (Crossroads(CAUSE, MAX_CROSSROADS), seen(iterations=7), Quiet(), EndDegraded(None)),
]


@pytest.mark.parametrize(("state", "observations", "next_state", "command"), TRANSITIONS)
def test_each_transition_yields_its_next_state_and_command(
    state: DecisionState,
    observations: Observations,
    next_state: DecisionState,
    command: Command | None,
) -> None:
    assert advance(state, observations) == (next_state, command)


def test_an_ignored_context_demand_expires_its_grace_then_degrades() -> None:
    state: DecisionState = Quiet()
    history: list[tuple[DecisionState, Command | None]] = []
    for iteration in range(3, 10):
        state, command = advance(state, seen(iterations=iteration, pressure=CAUSE))
        history.append((state, command))

    assert history == [
        (Demanded(CAUSE, 3), Ask(DECISION_NUDGE.format(cause=CAUSE))),
        (Demanded(CAUSE, 3), None),
        (Demanded(CAUSE, 3), None),
        (Demanded(CAUSE, 3), None),
        (Crossroads(CAUSE, 1), None),
        (Crossroads(CAUSE, 2), None),
        (Quiet(), EndDegraded(None)),
    ]


def test_ignored_narration_pressure_follows_the_same_schedule_backdated_one_step() -> None:
    state: DecisionState = Quiet()
    history: list[tuple[DecisionState, Command | None]] = []
    state, command = advance(state, seen(iterations=1))
    history.append((state, command))
    for iteration in range(2, 8):
        state, command = advance(
            state,
            seen(
                iterations=iteration,
                pressure=NARRATION_PRESSURE,
                narration=f"musing {iteration - 2}",
            ),
        )
        history.append((state, command))

    cause = NARRATION_PRESSURE
    assert history == [
        (Quiet(), None),
        (Demanded(cause, 1), Ask(DECISION_NUDGE.format(cause=cause))),
        (Demanded(cause, 1), None),
        (Demanded(cause, 1), None),
        (Crossroads(cause, 1), None),
        (Crossroads(cause, 2), None),
        (Quiet(), EndDegraded("musing 5")),
    ]


def test_deciding_resets_the_machine_and_a_later_pressure_asks_again() -> None:
    state: DecisionState = Quiet()
    state, _ = advance(state, seen(iterations=2, pressure=CAUSE))
    state, _ = advance(state, seen(undecided=False, iterations=3))
    assert state == Quiet()

    state, command = advance(state, seen(iterations=9, pressure=CAUSE))
    assert state == Demanded(CAUSE, 9)
    assert command == Ask(DECISION_NUDGE.format(cause=CAUSE))
