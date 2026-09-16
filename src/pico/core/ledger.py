from dataclasses import dataclass

from pico.session import (
    PlanSet,
    PlanStepCompleted,
    Session,
    ToolCallRecorded,
)


@dataclass(frozen=True)
class Fact:
    id: int
    content: str
    source: str


def facts(session: Session) -> list[Fact]:
    return [
        Fact(id=seq, content=event.result, source=event.name)
        for seq, event in session.records()
        if isinstance(event, ToolCallRecorded) and not event.is_error
    ]


@dataclass(frozen=True)
class PlanStep:
    text: str
    done: bool


@dataclass(frozen=True)
class Plan:
    steps: tuple[PlanStep, ...]


def plan(session: Session) -> Plan | None:
    steps: list[PlanStep] | None = None
    for event in session.events():
        match event:
            case PlanSet(steps=texts):
                steps = [PlanStep(text=text, done=False) for text in texts]
            case PlanStepCompleted(index=index):
                if steps is not None and 0 <= index < len(steps):
                    steps[index] = PlanStep(text=steps[index].text, done=True)
            case _:
                pass
    return None if steps is None else Plan(steps=tuple(steps))


def render_plan(current: Plan) -> str:
    return "\n".join(
        f"[{'x' if step.done else ' '}] {index}. {step.text}"
        for index, step in enumerate(current.steps)
    )
