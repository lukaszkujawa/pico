import json
from collections.abc import Mapping
from typing import Literal

from pico.core.ledger import BOOKKEEPING_TOOLS, Fact, facts, plan, render_call, render_plan
from pico.llm.types import Message, Role, ToolCall, ToolResult
from pico.session import Session

RenderLevel = Literal["full", "handle"]

COMPLETION_RESERVE_FRACTION = 0.25
COMPLETION_RESERVE_CAP = 4096

SYSTEM_PROMPT = (
    "You are Pico, a tiny agent solving big problems. "
    "Older messages fall out of your context, but facts are kept: record important "
    "findings with note, rediscover facts with search_facts, and recover any fact "
    "in full with read_fact."
)

_HANDLE_PREVIEW_CHARS = 200
_INDEX_FACTS = 20
_INDEX_LINE_CHARS = 90


def _fact_preview(content: str, limit: int) -> str:
    return " ".join(content.split())[:limit]


def _index_line(fact: Fact) -> str:
    signature = render_call(fact.source, fact.arguments)
    prefix = f"[{fact.id}] {signature}: "
    preview = _fact_preview(fact.content, max(0, _INDEX_LINE_CHARS - len(prefix)))
    return f"{prefix}{preview}"


def _newest_per_call(all_facts: list[Fact]) -> list[Fact]:
    newest: dict[str, Fact] = {}
    for fact in all_facts:
        newest[f"{fact.source}{json.dumps(fact.arguments, sort_keys=True)}"] = fact
    return list(newest.values())


def fact_index(all_facts: list[Fact]) -> str:
    if not all_facts:
        return ""
    distinct = _newest_per_call(all_facts)
    shown = distinct[-_INDEX_FACTS:]
    overflow = len(distinct) - len(shown)
    lines = [_index_line(fact) for fact in shown]
    if overflow:
        lines.append(f"+{overflow} earlier facts")
    lines.append("Call read_fact(id) to recover any fact in full.")
    return "\n".join(lines)


def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / chars_per_token))


def render_tool_result(
    content: str, fact_id: int | None, level: RenderLevel, signature: str = ""
) -> str:
    if level == "full":
        return content
    tokens = estimate_tokens(content)
    lead = f"{signature} " if signature else ""
    if fact_id is None:
        summary = f"[{lead}result truncated — {len(content)} chars, {tokens} tokens]"
    else:
        summary = (
            f"[fact {fact_id} {lead}truncated — {len(content)} chars, {tokens} tokens "
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


def message_tokens(message: Message, chars_per_token: float = 4.0) -> int:
    if message.role is Role.TOOL:
        assert message.tool_result is not None
        return estimate_tokens(message.tool_result.content, chars_per_token)
    total = estimate_tokens(message.content, chars_per_token)
    return total + sum(
        estimate_tokens(tool_call_text(call), chars_per_token) for call in message.tool_calls
    )


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


def _call_arguments(body: list[Message], index: int, tool_call_id: str) -> Mapping[str, object]:
    for call in body[index - 1].tool_calls:
        if call.id == tool_call_id:
            return call.arguments
    return {}


def _demote_to_handle(body: list[Message], index: int) -> Message:
    message = body[index]
    assert message.tool_result is not None
    result = message.tool_result
    is_bookkeeping = result.name in BOOKKEEPING_TOOLS
    fact_id = None if is_bookkeeping else int(result.tool_call_id)
    signature = ""
    if result.name and not is_bookkeeping:
        signature = render_call(result.name, _call_arguments(body, index, result.tool_call_id))
    return Message(
        role=Role.TOOL,
        tool_result=ToolResult(
            tool_call_id=result.tool_call_id,
            content=render_tool_result(result.content, fact_id, "handle", signature),
            is_error=False,
            name=result.name,
        ),
    )


def _pinned_positions(messages: list[Message]) -> list[int]:
    users = [position for position, message in enumerate(messages) if message.role is Role.USER]
    return sorted({users[0], users[-1]}) if users else []


def recency_window(
    messages: list[Message], budget: int, chars_per_token: float = 4.0
) -> list[Message]:
    pinned = _pinned_positions(messages)
    positions = [position for position in range(len(messages)) if position not in pinned]
    body = [messages[position] for position in positions]
    starts = _unit_starts(body)
    if len(starts) > RECENT_UNITS:
        keep = starts[-RECENT_UNITS]
        positions, body = positions[keep:], body[keep:]

    total = sum(message_tokens(messages[position], chars_per_token) for position in pinned)
    total += sum(message_tokens(message, chars_per_token) for message in body)

    for index, message in enumerate(body):
        if total <= budget:
            break
        if message.role is not Role.TOOL:
            continue
        assert message.tool_result is not None
        if message.tool_result.is_error:
            continue
        demoted = _demote_to_handle(body, index)
        total += message_tokens(demoted, chars_per_token) - message_tokens(message, chars_per_token)
        body[index] = demoted

    cut = 0
    while cut < len(body) and total > budget:
        next_cut = _unit_end(body, cut)
        total -= sum(message_tokens(message, chars_per_token) for message in body[cut:next_cut])
        cut = next_cut

    window = [(position, messages[position]) for position in pinned]
    window += list(zip(positions[cut:], body[cut:], strict=True))
    window.sort(key=lambda entry: entry[0])
    return [message for _, message in window]


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
    budget -= message_tokens(briefing, chars_per_token)
    return [briefing, *recency_window(session.messages(), budget, chars_per_token)]
