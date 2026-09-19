from dataclasses import dataclass, field

DEFAULT_CHARS_PER_TOKEN = 4.0


@dataclass
class Degradation:
    demoted: set[int] = field(default_factory=set[int])
    cut: int = 0


@dataclass(frozen=True)
class Nudge:
    text: str


@dataclass(frozen=True)
class Restrict:
    allowed: tuple[str, ...]
    text: str
    rejection: str


@dataclass(frozen=True)
class Running:
    pass


@dataclass(frozen=True)
class WindingDown:
    cause: str


@dataclass(frozen=True)
class LastWords:
    cause: str


@dataclass(frozen=True)
class Answered:
    content: str
    cause: str | None = None


@dataclass(frozen=True)
class Failed:
    reason: str


@dataclass(frozen=True)
class Cancelled:
    pass


RunState = Running | WindingDown | LastWords | Answered | Failed | Cancelled


@dataclass
class GenerationState:
    actionless_generations: int = 0
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN
    degradation: Degradation = field(default_factory=Degradation)
    last_narration: str | None = None
    narration_pressure: bool = False


@dataclass
class DispatchState:
    invalid_action_attempts: int = 0


@dataclass
class StepState:
    attempts: dict[tuple[tuple[str, ...], int], int] = field(
        default_factory=dict[tuple[tuple[str, ...], int], int]
    )
