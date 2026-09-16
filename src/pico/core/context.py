from typing import Literal

from pico.llm.types import Message, Role, ToolResult
from pico.session import Session

RenderLevel = Literal["full", "handle"]

COMPLETION_RESERVE_FRACTION = 0.25

SYSTEM_PROMPT = "You are Pico, a tiny agent solving big problems."

_HANDLE_PREVIEW_CHARS = 200


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


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
    return int(context_size * (1 - COMPLETION_RESERVE_FRACTION))


def _message_tokens(message: Message) -> int:
    if message.role is Role.TOOL:
        assert message.tool_result is not None
        return estimate_tokens(message.tool_result.content)
    return estimate_tokens(message.content)


def render_messages(session: Session, context_size: int) -> list[Message]:
    messages = session.messages()
    budget = prompt_budget(context_size)
    total = sum(_message_tokens(message) for message in messages)
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

        before = estimate_tokens(message.tool_result.content)
        new_content = render_tool_result(message.tool_result.content, fact_id, "handle")
        after = estimate_tokens(new_content)
        result[position] = Message(
            role=Role.TOOL,
            tool_result=ToolResult(
                tool_call_id=message.tool_result.tool_call_id,
                content=new_content,
                is_error=message.tool_result.is_error,
            ),
        )
        total += after - before

    return result
