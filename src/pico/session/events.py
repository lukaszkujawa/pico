from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class UserMessageRecorded:
    content: str


@dataclass(frozen=True)
class AssistantMessageRecorded:
    content: str
    thinking: str


@dataclass(frozen=True)
class ToolCallRecorded:
    name: str
    arguments: Mapping[str, object]
    result: str
    is_error: bool


@dataclass(frozen=True)
class PlanSet:
    steps: tuple[str, ...]


@dataclass(frozen=True)
class PlanStepCompleted:
    index: int


SessionEvent = (
    UserMessageRecorded | AssistantMessageRecorded | ToolCallRecorded | PlanSet | PlanStepCompleted
)
