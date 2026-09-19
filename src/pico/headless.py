import time
from dataclasses import dataclass

from pico.core.actions import register_actions
from pico.core.bus import Bus
from pico.core.events import (
    AnswerSettled,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    ToolCallFinished,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG, LoopRunner
from pico.core.loop.state import Answered
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.session import Session, UserMessageRecorded


@dataclass(frozen=True)
class TurnResult:
    answer: str | None
    iterations: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    duration_seconds: float
    error: str | None


def run_turn(
    llm: LLMClient, session: Session, context_size: int, prompt: str, vision: bool = False
) -> TurnResult:
    bus = Bus()
    subscriber = bus.subscribe()
    session.append(UserMessageRecorded(content=prompt))
    tools = ToolRegistry()
    register_actions(tools, session, vision=vision)
    runner = LoopRunner(llm, tools, bus, session, context_size, DEFAULT_LOOP_CONFIG)

    started = time.monotonic()
    runner.execute()
    duration = time.monotonic() - started

    tool_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    error: str | None = None
    for event in subscriber:
        match event:
            case GenerationCompleted(
                prompt_tokens=prompt_count, completion_tokens=completion_count
            ):
                prompt_tokens += prompt_count or 0
                completion_tokens += completion_count or 0
            case ToolCallFinished() | AnswerSettled():
                tool_calls += 1
            case ErrorOccurred(message=message):
                error = message
            case RunFinished() | RunCancelled():
                break
            case _:
                pass

    return TurnResult(
        answer=runner.state.content if isinstance(runner.state, Answered) else None,
        iterations=runner.iterations,
        tool_calls=tool_calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        duration_seconds=duration,
        error=error,
    )
