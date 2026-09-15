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


SessionEvent = UserMessageRecorded | AssistantMessageRecorded | ToolCallRecorded
