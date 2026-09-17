import json
from typing import Literal

from pico.core.ledger import Fact, facts, plan, render_plan
from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session import Session

RenderLevel = Literal["full", "handle"]

COMPLETION_RESERVE_FRACTION = 0.25
COMPLETION_RESERVE_CAP = 4096

SYSTEM_PROMPT = "You are Pico, a tiny agent solving big problems."

_HANDLE_PREVIEW_CHARS = 200
_INDEX_FACTS = 20
_INDEX_PREVIEW_CHARS = 72


def _fact_preview(content: str) -> str:
    return " ".join(content.split())[:_INDEX_PREVIEW_CHARS]


def fact_index(all_facts: list[Fact]) -> str:
    if not all_facts:
        return ""
    shown = all_facts[-_INDEX_FACTS:]
    overflow = len(all_facts) - len(shown)
    lines = [f"[{fact.id}] {fact.source}: {_fact_preview(fact.content)}" for fact in shown]
    if overflow:
        lines.append(f"+{overflow} earlier facts")
    lines.append("Call read_fact(id) to recover any fact in full.")
    return "\n".join(lines)


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


RECENT_UNITS = 8


def _unit_starts(messages: list[Message]) -> list[int]:
    starts: list[int] = []
    position = 0
    while position < len(messages):
        starts.append(position)
        position = _unit_end(messages, position)
    return starts


def _demote_to_handle(message: Message) -> Message:
    assert message.tool_result is not None
    fact_id = int(message.tool_result.tool_call_id)
    return Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id=message.tool_result.tool_call_id,
            content=render_tool_result(message.tool_result.content, fact_id, "handle"),
            is_error=False,
        ),
    )


def recency_window(
    messages: list[Message], budget: int, chars_per_token: float = 4.0
) -> list[Message]:
    if not messages:
        return []
    protected = _protected_start(messages)
    starts = _unit_starts(messages)
    cap_start = starts[-RECENT_UNITS] if len(starts) > RECENT_UNITS else 0
    start = min(cap_start, protected)
    window = list(messages[start:])
    total = sum(_message_tokens(message, chars_per_token) for message in window)

    for position, message in enumerate(window):
        if total <= budget:
            break
        if message.role is not Role.TOOL:
            continue
        assert message.tool_result is not None
        if message.tool_result.is_error:
            continue
        demoted = _demote_to_handle(message)
        total += _message_tokens(demoted, chars_per_token) - _message_tokens(
            message, chars_per_token
        )
        window[position] = demoted

    tail_start = protected - start
    cut = 0
    while cut < tail_start and total > budget:
        next_cut = _unit_end(window, cut)
        total -= sum(_message_tokens(message, chars_per_token) for message in window[cut:next_cut])
        cut = next_cut
    return window[cut:]


def _briefing(session: Session) -> Message | None:
    sections: list[str] = []
    current_plan = plan(session)
    if current_plan is not None:
        sections.append(
            f"Your current plan:\n{render_plan(current_plan)}\n"
            "Keep it current with set_plan and complete_step."
        )
    index = fact_index(facts(session))
    if index:
        sections.append(f"Facts gathered so far:\n{index}")
    if not sections:
        return None
    return Message(role=Role.USER, content="\n\n".join(sections))


def compile_context(
    session: Session,
    context_size: int,
    overhead_tokens: int = 0,
    chars_per_token: float = 4.0,
) -> list[Message]:
    budget = prompt_budget(context_size) - overhead_tokens
    briefing = _briefing(session)
    if briefing is None:
        return recency_window(session.messages(), budget, chars_per_token)
    budget -= _message_tokens(briefing, chars_per_token)
    return [briefing, *recency_window(session.messages(), budget, chars_per_token)]
