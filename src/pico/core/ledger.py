from dataclasses import dataclass

from pico.session import Session, ToolCallRecorded, UserMessageRecorded


@dataclass(frozen=True)
class Fact:
    id: int
    content: str
    source: str


@dataclass(frozen=True)
class Goal:
    content: str


def facts(session: Session) -> list[Fact]:
    return [
        Fact(id=seq, content=event.result, source=event.name)
        for seq, event in session.records()
        if isinstance(event, ToolCallRecorded) and not event.is_error
    ]


def goal(session: Session) -> Goal | None:
    latest: Goal | None = None
    for event in session.events():
        if isinstance(event, UserMessageRecorded):
            latest = Goal(content=event.content)
    return latest
