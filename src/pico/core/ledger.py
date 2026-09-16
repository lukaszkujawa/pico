from dataclasses import dataclass

from pico.session import Session, ToolCallRecorded, UserMessageRecorded


@dataclass(frozen=True)
class Fact:
    index: int
    content: str
    source: str


@dataclass(frozen=True)
class Goal:
    content: str


def facts(session: Session) -> list[Fact]:
    result: list[Fact] = []
    for event in session.events():
        if isinstance(event, ToolCallRecorded) and not event.is_error:
            result.append(Fact(index=len(result), content=event.result, source=event.name))
    return result


def goal(session: Session) -> Goal | None:
    latest: Goal | None = None
    for event in session.events():
        if isinstance(event, UserMessageRecorded):
            latest = Goal(content=event.content)
    return latest
