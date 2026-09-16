import json
from typing import Literal

from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session import Session

RenderLevel = Literal["full", "handle"]

COMPLETION_RESERVE_FRACTION = 0.25
COMPLETION_RESERVE_CAP = 4096

SYSTEM_PROMPT = "You are Pico, a tiny agent solving big problems."

_HANDLE_PREVIEW_CHARS = 200
_MARKER_FACT_IDS = 12


def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))


def render_tool_result(content: str, fact_id: int, level: RenderLevel) -> str:
    if level == "full":
        return content
    tokens = estimate_tokens(content)
    summary = (
        f"[fact {fact_id} truncated — {len(content)} chars, {tokens} tokens "
        f"— call read_fact({fact_id}) for the full content]"
    )
    preview = content[:_HANDLE_PREVIEW_CHARS]
    return f"{summary} {preview}"


def prompt_budget(context_size: int) -> int:
    reserve = min(int(context_size * COMPLETION_RESERVE_FRACTION), COMPLETION_RESERVE_CAP)
    return context_size - reserve


def tool_call_text(call: ToolCall) -> str:
    return f"{call.name}{json.dumps(call.arguments, sort_keys=True)}"


def message_text(message: Message) -> str:
    if message.role is Role.TOOL:
        assert message.tool_result is not None
        return message.tool_result.content
    return message.content + "".join(tool_call_text(call) for call in message.tool_calls)


def _message_tokens(message: Message, chars_per_token: float) -> int:
    if message.role is Role.TOOL:
        assert message.tool_result is not None
        return estimate_tokens(message.tool_result.content, chars_per_token)
    total = estimate_tokens(message.content, chars_per_token)
    return total + sum(
        estimate_tokens(tool_call_text(call), chars_per_token) for call in message.tool_calls
    )


def _protected_start(messages: list[Message]) -> int:
    for position in reversed(range(len(messages))):
        if messages[position].role is Role.USER:
            return position
    return 0


def _unit_end(messages: list[Message], start: int) -> int:
    end = start + 1
    while end < len(messages) and messages[end].role is Role.TOOL:
        end += 1
    return end


def elision_marker(messages: list[Message]) -> Message:
    fact_ids = [
        int(message.tool_result.tool_call_id)
        for message in messages
        if message.role is Role.TOOL
        and message.tool_result is not None
        and not message.tool_result.is_error
    ]
    shown = fact_ids[-_MARKER_FACT_IDS:]
    overflow = len(fact_ids) - len(shown)
    calls = ", ".join(f"read_fact({fact_id})" for fact_id in shown)
    if overflow:
        calls += f", +{overflow} more"
    evidence = f" — their evidence remains available: {calls}" if fact_ids else ""
    return Message(
        role=Role.USER,
        content=(f"[{len(messages)} earlier messages elided to fit the context budget{evidence}]"),
    )


def elide(messages: list[Message], budget: int, chars_per_token: float = 4.0) -> list[Message]:
    def cost(cut: int) -> int:
        rendered = messages[cut:] if cut == 0 else [elision_marker(messages[:cut]), *messages[cut:]]
        return sum(_message_tokens(message, chars_per_token) for message in rendered)

    protected = _protected_start(messages)
    cut = 0
    while cut < protected and cost(cut) > budget:
        cut = _unit_end(messages, cut)

    return messages if cut == 0 else [elision_marker(messages[:cut]), *messages[cut:]]


def render_messages(
    session: Session,
    context_size: int,
    overhead_tokens: int = 0,
    chars_per_token: float = 4.0,
) -> list[Message]:
    messages = session.messages()
    budget = prompt_budget(context_size) - overhead_tokens
    total = sum(_message_tokens(message, chars_per_token) for message in messages)
    if total <= budget:
        return messages

    result = list(messages)
    for position, message in enumerate(result):
        if message.role is not Role.TOOL:
            continue
        assert message.tool_result is not None
        if message.tool_result.is_error:
            continue
        if total <= budget:
            break
        fact_id = int(message.tool_result.tool_call_id)

        before = estimate_tokens(message.tool_result.content, chars_per_token)
        new_content = render_tool_result(message.tool_result.content, fact_id, "handle")
        after = estimate_tokens(new_content, chars_per_token)
        result[position] = Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id=message.tool_result.tool_call_id,
                content=new_content,
                is_error=message.tool_result.is_error,
            ),
        )
        total += after - before

    return elide(result, budget, chars_per_token)
