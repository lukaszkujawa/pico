from dataclasses import dataclass

from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    GenerationCompleted,
    ToolCallArgumentsDelta,
)
from pico.core.loop.policy import (
    NARRATION_PRESSURE,
    demand,
    pressure,
    record_narration,
    undecided,
)
from pico.core.loop.prompt import Prompt, assemble, reconcile
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.signals import Nudge
from pico.core.loop.state import LastWords, WindingDown
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


def stream(runner: LoopRunner, prompt: Prompt) -> Generation | None:
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
                        pressure=runner.decision.crossroads or pressure(runner) is not None,
                    )
                )
                if prompt_tokens:
                    reconcile(runner, prompt, prompt_tokens)

    if thinking_id is not None:
        runner.bus.publish(AssistantThinkingFinished(id=thinking_id))
    if text_id is not None:
        runner.bus.publish(AssistantTextFinished(id=text_id))

    if cancelled:
        return None
    return Generation(text=text, thinking=thinking, tool_calls=tool_calls)


def record(runner: LoopRunner, generation: Generation) -> StepOutcome:
    text = generation.text
    if text or generation.thinking:
        runner.session.append(AssistantMessageRecorded(content=text, thinking=generation.thinking))
    if text.strip():
        record_narration(runner, text)

    if generation.tool_calls:
        runner.generation.actionless_generations = 0
        runner.pending_tool_calls = generation.tool_calls
        return "continue"

    if isinstance(runner.state, LastWords):
        return "done"
    if undecided(runner):
        runner.generation.actionless_generations = 0
        demand(runner, NARRATION_PRESSURE)
        return "continue"
    runner.generation.actionless_generations += 1
    if runner.generation.actionless_generations >= MAX_ACTIONLESS_GENERATIONS:
        runner.fail(
            f"run stopped: {MAX_ACTIONLESS_GENERATIONS} generations without a tool call "
            "while the plan has unfinished steps"
        )
        return "done"
    runner.emit(Nudge(NO_ACTION_NUDGE))
    return "continue"


def generation_step(runner: LoopRunner) -> StepOutcome:
    if isinstance(runner.state, WindingDown):
        runner.state = LastWords(runner.state.cause)
    generation = stream(runner, assemble(runner))
    if generation is None:
        return "cancelled"
    return record(runner, generation)
