from dataclasses import dataclass

from pico.core.context import prompt_budget, transcript_units
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BudgetExceeded,
    GenerationCompleted,
    ToolCallArgumentsDelta,
)
from pico.core.loop.decision import Crossroads
from pico.core.loop.policy import budget_remaining, pressure, restriction, undecided
from pico.core.loop.prompt import Prompt, assemble, reconcile
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.signals import Nudge
from pico.core.loop.state import LastWords, Running, WindingDown
from pico.llm.types import (
    GenerationComplete,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
)
from pico.session import AssistantMessageRecorded

MAX_ACTIONLESS_GENERATIONS = 3

NO_ACTION_NUDGE = (
    "you wrote text but took no action, and your plan has unfinished steps "
    "— call a tool to continue, or finish with answer"
)


@dataclass(frozen=True)
class Generation:
    text: str
    thinking: str
    tool_calls: list[ToolCall]


@dataclass(frozen=True)
class Recorded:
    outcome: StepOutcome
    actionless: int
    pressed: bool = False
    nudge: str | None = None
    failure: str | None = None


def stream(runner: LoopRunner, prompt: Prompt, pressured: bool) -> Generation | None:
    text = ""
    thinking = ""
    tool_calls: list[ToolCall] = []
    text_id: str | None = None
    thinking_id: str | None = None
    cancelled = False
    runner.tool_call_pane_ids = {}

    for event in runner.llm.stream(prompt.messages, prompt.specs):
        if runner.cancel.is_set():
            cancelled = True
            break
        match event:
            case ThinkingDelta(text=chunk):
                if thinking_id is None:
                    thinking_id = runner.new_id()
                    runner.bus.publish(AssistantThinkingStarted(id=thinking_id))
                thinking += chunk
                runner.bus.publish(AssistantThinkingDelta(id=thinking_id, text=chunk))
            case TextDelta(text=chunk):
                if text_id is None:
                    text_id = runner.new_id()
                    runner.bus.publish(AssistantTextStarted(id=text_id))
                text += chunk
                runner.bus.publish(AssistantTextDelta(id=text_id, text=chunk))
            case ToolCallDelta(id=call_id, name=name, arguments_delta=arguments_delta):
                pane_id = runner.tool_call_pane_ids.get(call_id)
                if pane_id is None:
                    pane_id = runner.new_id()
                    runner.tool_call_pane_ids[call_id] = pane_id
                runner.bus.publish(
                    ToolCallArgumentsDelta(id=pane_id, name=name, text=arguments_delta)
                )
            case ToolCallReady(tool_call=tool_call):
                tool_calls.append(tool_call)
            case GenerationComplete(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ):
                runner.bus.publish(
                    GenerationCompleted(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        iteration=runner.iterations,
                        pressure=pressured,
                    )
                )
                if prompt_tokens:
                    runner.generation.chars_per_token = reconcile(prompt.sent_chars, prompt_tokens)

    if thinking_id is not None:
        runner.bus.publish(AssistantThinkingFinished(id=thinking_id))
    if text_id is not None:
        runner.bus.publish(AssistantTextFinished(id=text_id))

    if cancelled:
        return None
    return Generation(text=text, thinking=thinking, tool_calls=tool_calls)


def record(generation: Generation, dying: bool, undecided: bool, actionless: int) -> Recorded:
    if generation.tool_calls:
        return Recorded("continue", actionless=0)
    if dying:
        return Recorded("done", actionless=actionless)
    if undecided:
        return Recorded("continue", actionless=0, pressed=True)
    attempts = actionless + 1
    if attempts >= MAX_ACTIONLESS_GENERATIONS:
        return Recorded(
            "done",
            actionless=attempts,
            failure=(
                f"run stopped: {MAX_ACTIONLESS_GENERATIONS} generations without a tool call "
                "while the plan has unfinished steps"
            ),
        )
    return Recorded("continue", actionless=attempts, nudge=NO_ACTION_NUDGE)


def generation_step(runner: LoopRunner) -> StepOutcome:
    if isinstance(runner.state, WindingDown):
        runner.state = LastWords(runner.state.cause)
    nudges = runner.take_nudges()
    joined = "\n\n".join(nudge.text for nudge in nudges) if nudges else None
    active = restriction(runner.state, runner.decision, joined)
    runner.active_restriction = active
    prompt = assemble(
        runner.session,
        runner.tools,
        runner.depth,
        runner.context_size,
        runner.generation.chars_per_token,
        active,
        joined,
    )
    budget = prompt_budget(runner.context_size)
    if prompt.estimated_tokens > budget:
        runner.bus.publish(BudgetExceeded(estimated=prompt.estimated_tokens, budget=budget))
    running = isinstance(runner.state, Running)
    remaining = budget_remaining(runner.iterations, runner.config.max_steps, runner.depth, running)
    pressured = (
        isinstance(runner.decision, Crossroads)
        or pressure(transcript_units(runner.session.messages()), remaining) is not None
    )
    generation = stream(runner, prompt, pressured)
    if generation is None:
        return "cancelled"
    if generation.text or generation.thinking:
        runner.session.append(
            AssistantMessageRecorded(content=generation.text, thinking=generation.thinking)
        )
    if generation.text.strip():
        runner.generation.last_narration = generation.text
    if generation.tool_calls:
        runner.pending_tool_calls = generation.tool_calls
    recorded = record(
        generation,
        isinstance(runner.state, LastWords),
        isinstance(runner.state, Running) and undecided(runner.session),
        runner.generation.actionless_generations,
    )
    runner.generation.actionless_generations = recorded.actionless
    if recorded.pressed:
        runner.generation.narration_pressure = True
    if recorded.nudge is not None:
        runner.emit(Nudge(recorded.nudge))
    if recorded.failure is not None:
        runner.fail(recorded.failure)
    return recorded.outcome
