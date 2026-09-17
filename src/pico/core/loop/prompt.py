import json
from dataclasses import dataclass

from pico.core.actions import MAX_DELEGATE_DEPTH, vocabulary
from pico.core.context import (
    SYSTEM_PROMPT,
    compile_context,
    estimate_tokens,
    message_text,
    message_tokens,
    prompt_budget,
)
from pico.core.events import BudgetExceeded
from pico.core.loop.policy import restriction
from pico.core.loop.runner import LoopRunner
from pico.core.loop.signals import Nudge, Signal
from pico.llm.types import Message, Role, ToolSpec

MIN_CHARS_PER_TOKEN = 2.0
MAX_CHARS_PER_TOKEN = 6.0


@dataclass(frozen=True)
class Prompt:
    messages: list[Message]
    specs: list[ToolSpec]
    sent_chars: int


def specs_text(specs: list[ToolSpec]) -> str:
    return json.dumps(
        [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in specs
        ],
        sort_keys=True,
    )


def _nudge_text(signal: Signal | None) -> str | None:
    return signal.text if isinstance(signal, Nudge) else None


def assemble(runner: LoopRunner) -> Prompt:
    signal = runner.take_signal()
    active = restriction(runner, signal)
    runner.active_restriction = active
    runner.last_words = runner.dying_of is not None

    specs = vocabulary(runner.tools, runner.depth, None if active is None else active.allowed)
    nudge = active.text if active is not None else _nudge_text(signal)
    preamble = [Message(role=Role.SYSTEM, content=SYSTEM_PROMPT)]
    postamble = [] if nudge is None else [Message(role=Role.USER, content=nudge)]

    chars_per_token = runner.generation.chars_per_token
    overhead_text = specs_text(specs) + "".join(
        message_text(message) for message in [*preamble, *postamble]
    )
    overhead_tokens = estimate_tokens(overhead_text, chars_per_token)
    conversation = compile_context(
        runner.session,
        runner.context_size,
        overhead_tokens,
        chars_per_token,
        runner.depth < MAX_DELEGATE_DEPTH,
    )
    estimated = overhead_tokens + sum(
        message_tokens(message, chars_per_token) for message in conversation
    )
    budget = prompt_budget(runner.context_size)
    if estimated > budget:
        runner.bus.publish(BudgetExceeded(estimated=estimated, budget=budget))

    return Prompt(
        messages=[*preamble, *conversation, *postamble],
        specs=specs,
        sent_chars=len(overhead_text) + sum(len(message_text(message)) for message in conversation),
    )


def reconcile(runner: LoopRunner, prompt: Prompt, prompt_tokens: int) -> None:
    observed = prompt.sent_chars / prompt_tokens
    runner.generation.chars_per_token = min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, observed))
