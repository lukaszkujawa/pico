import json
from collections.abc import Mapping
from dataclasses import dataclass

from pico.core.actions import MAX_DELEGATE_DEPTH, vocabulary
from pico.core.ledger import BOOKKEEPING_TOOLS, fact_index, facts, plan, render_call, render_plan
from pico.core.loop.state import Degradation, Restrict
from pico.core.tools import ToolRegistry
from pico.llm.budget import estimate_tokens, prompt_budget
from pico.llm.types import Message, Role, ToolCall, ToolResult, ToolSpec
from pico.session import Session

SYSTEM_PROMPT = (
    "You are Pico, a tiny agent solving big problems. "
    "Older messages fall out of your context, but facts are kept: record important "
    "findings with note, rediscover facts with search_facts, and recover any fact "
    "in full with read_fact."
)

PLAN_ORCHESTRATED_HINT = (
    "The runtime runs each unfinished step for you in a fresh agent and marks it done "
    "when that agent answers. Between steps, revise the plan with set_plan if what came "
    "back changes it, or finish with answer."
)
PLAN_INLINE_HINT = "Keep it current with set_plan and complete_step."

MIN_CHARS_PER_TOKEN = 2.0
MAX_CHARS_PER_TOKEN = 6.0

_HANDLE_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class Prompt:
    messages: list[Message]
    specs: list[ToolSpec]
    sent_chars: int
    estimated_tokens: int


def assemble(
    session: Session,
    tools: ToolRegistry,
    depth: int,
    context_size: int,
    chars_per_token: float,
    active: Restrict | None,
    nudge: str | None,
    degradation: Degradation | None = None,
) -> Prompt:
    specs = vocabulary(tools, depth, None if active is None else active.allowed)
    text = active.text if active is not None else nudge
    preamble = [Message(role=Role.SYSTEM, content=SYSTEM_PROMPT)]
    postamble = [] if text is None else [Message(role=Role.USER, content=text)]

    overhead_text = specs_text(specs) + "".join(
        message_text(message) for message in [*preamble, *postamble]
    )
    overhead_tokens = estimate_tokens(overhead_text, chars_per_token)
    conversation = compile_context(
        session,
        context_size,
        overhead_tokens,
        chars_per_token,
        depth < MAX_DELEGATE_DEPTH,
        degradation,
    )
    estimated = overhead_tokens + sum(
        message_tokens(message, chars_per_token) for message in conversation
    )

    return Prompt(
        messages=[*preamble, *conversation, *postamble],
        specs=specs,
        sent_chars=len(overhead_text) + sum(len(message_text(message)) for message in conversation),
        estimated_tokens=estimated,
    )


def specs_text(specs: list[ToolSpec]) -> str:
    return json.dumps(
        [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in specs
        ],
        sort_keys=True,
    )


def compile_context(
    session: Session,
    context_size: int,
    overhead_tokens: int = 0,
    chars_per_token: float = 4.0,
    orchestrated: bool = False,
    degradation: Degradation | None = None,
) -> list[Message]:
    budget = prompt_budget(context_size) - overhead_tokens
    briefing = _briefing(session, orchestrated)
    if briefing is None:
        return recency_window(session.messages(), budget, chars_per_token, degradation)
    budget -= message_tokens(briefing, chars_per_token)
    window = recency_window(session.messages(), budget, chars_per_token, degradation)
    return [*window, briefing]


def _briefing(session: Session, orchestrated: bool = False) -> Message | None:
    sections: list[str] = []
    current_plan = plan(session)
    if current_plan is not None:
        hint = PLAN_ORCHESTRATED_HINT if orchestrated else PLAN_INLINE_HINT
        sections.append(f"Your current plan:\n{render_plan(current_plan)}\n{hint}")
    index = fact_index(facts(session))
    if index:
        sections.append(f"Facts gathered so far:\n{index}")
    if not sections:
        return None
    return Message(role=Role.USER, content="\n\n".join(sections))


def recency_window(
    messages: list[Message],
    budget: int,
    chars_per_token: float = 4.0,
    degradation: Degradation | None = None,
) -> list[Message]:
    if degradation is None:
        degradation = Degradation()
    pinned = _pinned_positions(messages)
    positions = [
        position
        for position in range(len(messages))
        if position not in pinned and position >= degradation.cut
    ]
    body = [messages[position] for position in positions]
    for index, position in enumerate(positions):
        if position in degradation.demoted:
            body[index] = _demote_to_handle(body, index)

    total = sum(message_tokens(messages[position], chars_per_token) for position in pinned)
    total += sum(message_tokens(message, chars_per_token) for message in body)

    for index, message in enumerate(body):
        if total <= budget:
            break
        if message.role is not Role.TOOL or positions[index] in degradation.demoted:
            continue
        assert message.tool_result is not None
        if message.tool_result.is_error:
            continue
        demoted = _demote_to_handle(body, index)
        total += message_tokens(demoted, chars_per_token) - message_tokens(message, chars_per_token)
        body[index] = demoted
        degradation.demoted.add(positions[index])

    cut = 0
    while cut < len(body) and total > budget:
        next_cut = _unit_end(body, cut)
        total -= sum(message_tokens(message, chars_per_token) for message in body[cut:next_cut])
        cut = next_cut
    if cut:
        degradation.cut = positions[cut] if cut < len(positions) else len(messages)

    window = [(position, messages[position]) for position in pinned]
    window += list(zip(positions[cut:], body[cut:], strict=True))
    window.sort(key=lambda entry: entry[0])
    return [message for _, message in window]


def _pinned_positions(messages: list[Message]) -> list[int]:
    users = [position for position, message in enumerate(messages) if message.role is Role.USER]
    return sorted({users[0], users[-1]}) if users else []


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
            content=render_tool_result(result.content, fact_id, signature),
            is_error=False,
            name=result.name,
        ),
    )


def _call_arguments(body: list[Message], index: int, tool_call_id: str) -> Mapping[str, object]:
    for call in body[index - 1].tool_calls:
        if call.id == tool_call_id:
            return call.arguments
    return {}


def render_tool_result(content: str, fact_id: int | None, signature: str = "") -> str:
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


def _unit_end(messages: list[Message], start: int) -> int:
    end = start + 1
    while end < len(messages) and messages[end].role is Role.TOOL:
        end += 1
    return end


def _tool_call_text(call: ToolCall) -> str:
    return f"{call.name}{json.dumps(call.arguments, sort_keys=True)}"


def message_text(message: Message) -> str:
    if message.role is Role.TOOL:
        assert message.tool_result is not None
        return message.tool_result.content
    return message.content + "".join(_tool_call_text(call) for call in message.tool_calls)


def message_tokens(message: Message, chars_per_token: float = 4.0) -> int:
    if message.role is Role.TOOL:
        assert message.tool_result is not None
        return estimate_tokens(message.tool_result.content, chars_per_token)
    total = estimate_tokens(message.content, chars_per_token)
    return total + sum(
        estimate_tokens(_tool_call_text(call), chars_per_token) for call in message.tool_calls
    )


def transcript_fullness(
    messages: list[Message], context_size: int, chars_per_token: float = 4.0
) -> float:
    used = sum(message_tokens(message, chars_per_token) for message in messages)
    return used / prompt_budget(context_size)


def reconcile(sent_chars: int, prompt_tokens: int) -> float:
    observed = sent_chars / prompt_tokens
    return min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, observed))
