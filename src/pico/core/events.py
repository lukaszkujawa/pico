from collections.abc import Mapping
from dataclasses import dataclass

from pico.llm.types import ToolCall


@dataclass(frozen=True)
class RunStarted:
    pass


@dataclass(frozen=True)
class RunFinished:
    error: str | None = None


@dataclass(frozen=True)
class AssistantTextStarted:
    id: str


@dataclass(frozen=True)
class AssistantTextDelta:
    id: str
    text: str


@dataclass(frozen=True)
class AssistantTextFinished:
    id: str


@dataclass(frozen=True)
class AssistantThinkingStarted:
    id: str


@dataclass(frozen=True)
class AssistantThinkingDelta:
    id: str
    text: str


@dataclass(frozen=True)
class AssistantThinkingFinished:
    id: str


@dataclass(frozen=True)
class ToolCallStarted:
    id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolCallFinished:
    id: str
    tool_call: ToolCall
    result: str
    is_error: bool = False


@dataclass(frozen=True)
class GenerationCompleted:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True)
class BudgetExceeded:
    estimated: int
    actual: int
    budget: int


@dataclass(frozen=True)
class ErrorOccurred:
    message: str


@dataclass(frozen=True)
class RunCancelled:
    pass


BusEvent = (
    RunStarted
    | RunFinished
    | AssistantTextStarted
    | AssistantTextDelta
    | AssistantTextFinished
    | AssistantThinkingStarted
    | AssistantThinkingDelta
    | AssistantThinkingFinished
    | ToolCallStarted
    | ToolCallFinished
    | GenerationCompleted
    | BudgetExceeded
    | ErrorOccurred
    | RunCancelled
)
