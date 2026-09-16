import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pico.core.bus import Bus
from pico.core.errors import UnknownToolError
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
)
from pico.session import AssistantMessageRecorded, Session, ToolCallRecorded

StepOutcome = Literal["continue", "done", "cancelled"]

Step = Callable[["LoopRunner"], StepOutcome]


@dataclass(frozen=True)
class LoopConfig:
    steps: tuple[Step, ...]
    max_steps: int | None = None


class LoopRunner:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        bus: Bus,
        session: Session,
        config: LoopConfig,
        cancel: threading.Event | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self.session = session
        self.config = config
        self.cancel = cancel if cancel is not None else threading.Event()
        self.pending_tool_calls: list[ToolCall] = []
        self._next_id = 0

    def new_id(self) -> str:
        stream_id = str(self._next_id)
        self._next_id += 1
        return stream_id

    def execute(self) -> None:
        self.bus.publish(RunStarted())
        iterations = 0
        try:
            while self.config.max_steps is None or iterations < self.config.max_steps:
                iterations += 1
                outcome = self._run_iteration()
                if outcome == "cancelled":
                    self.bus.publish(RunCancelled())
                    return
                if outcome == "done":
                    break
        except LLMError as error:
            self.bus.publish(ErrorOccurred(message=str(error)))
            self.bus.publish(RunFinished(error=str(error)))
            return
        self.bus.publish(RunFinished())

    def _run_iteration(self) -> StepOutcome:
        for step in self.config.steps:
            outcome = step(self)
            if outcome != "continue":
                return outcome
        return "continue"


def stream_step(runner: LoopRunner) -> StepOutcome:
    text = ""
    thinking = ""
    tool_calls: list[ToolCall] = []
    text_id: str | None = None
    thinking_id: str | None = None
    cancelled = False

    for event in runner.llm.stream(runner.session.messages(), runner.tools.specs()):
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
            case ToolCallDelta(id=call_id, arguments_delta=arguments_delta):
                runner.bus.publish(
                    ToolCallArgumentsDelta(id=call_id, arguments_delta=arguments_delta)
                )
            case ToolCallReady(tool_call=tool_call):
                tool_calls.append(tool_call)
            case GenerationComplete():
                pass

    if thinking_id is not None:
        runner.bus.publish(AssistantThinkingFinished(id=thinking_id))
    if text_id is not None:
        runner.bus.publish(AssistantTextFinished(id=text_id))

    if cancelled:
        return "cancelled"

    runner.session.append(AssistantMessageRecorded(content=text, thinking=thinking))

    if not tool_calls:
        return "done"

    runner.pending_tool_calls = tool_calls
    return "continue"


def tool_call_step(runner: LoopRunner) -> StepOutcome:
    tool_calls = runner.pending_tool_calls
    runner.pending_tool_calls = []
    for call in tool_calls:
        runner.bus.publish(ToolCallStarted(id=call.id, name=call.name))
        try:
            output = runner.tools.execute(call)
            is_error = False
        except UnknownToolError as error:
            output = str(error)
            is_error = True
        runner.bus.publish(
            ToolCallFinished(id=call.id, tool_call=call, result=output, is_error=is_error)
        )
        runner.session.append(
            ToolCallRecorded(
                name=call.name, arguments=call.arguments, result=output, is_error=is_error
            )
        )
    return "continue"


DEFAULT_LOOP_CONFIG = LoopConfig(steps=(stream_step, tool_call_step))
