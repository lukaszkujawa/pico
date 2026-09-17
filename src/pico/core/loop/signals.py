from dataclasses import dataclass


@dataclass(frozen=True)
class Nudge:
    text: str


@dataclass(frozen=True)
class Restrict:
    allowed: tuple[str, ...]
    text: str
    rejection: str


Signal = Nudge | Restrict
