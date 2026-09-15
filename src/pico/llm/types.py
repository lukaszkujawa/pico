from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class Role(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, object]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    id: str
    name: str
    arguments_delta: str


@dataclass(frozen=True)
class ToolCallReady:
    tool_call: ToolCall


@dataclass(frozen=True)
class GenerationComplete:
    finish_reason: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


StreamEvent = TextDelta | ToolCallDelta | ToolCallReady | GenerationComplete
