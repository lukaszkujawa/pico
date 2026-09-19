import json
from dataclasses import dataclass

from pico.core.actions import MAX_DELEGATE_DEPTH, vocabulary
from pico.core.context import (
    SYSTEM_PROMPT,
    Degradation,
    compile_context,
    estimate_tokens,
    message_text,
    message_tokens,
)
from pico.core.loop.signals import Restrict
from pico.core.tools import ToolRegistry
from pico.llm.types import Message, Role, ToolSpec
from pico.session import Session

MIN_CHARS_PER_TOKEN = 2.0
MAX_CHARS_PER_TOKEN = 6.0


@dataclass(frozen=True)
class Prompt:
    messages: list[Message]
    specs: list[ToolSpec]
    sent_chars: int
    estimated_tokens: int


def specs_text(specs: list[ToolSpec]) -> str:
    return json.dumps(
        [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in specs
        ],
        sort_keys=True,
    )


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


def reconcile(sent_chars: int, prompt_tokens: int) -> float:
    observed = sent_chars / prompt_tokens
    return min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, observed))
