import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from pico.core.actions import (
    Answer,
    Delegate,
    InvalidActionError,
    register_delegate_actions,
)
from pico.core.bus import Bus
from pico.core.context import render_messages
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
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.ledger import facts
from pico.core.stuckness import assess
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
)
from pico.session import (
    AssistantMessageRecorded,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
)

StepOutcome = Literal["continue", "done", "cancelled"]

Step = Callable[["LoopRunner"], StepOutcome]

MAX_INVALID_ACTION_ATTEMPTS = 5
MAX_DELEGATE_STEPS = 10


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
        context_size: int,
        config: LoopConfig,
        cancel: threading.Event | None = None,
        id_source: Iterator[int] | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self.session = session
        self.context_size = context_size
        self.config = config
        self.cancel = cancel if cancel is not None else threading.Event()
        self.pending_tool_calls: list[ToolCall] = []
        self.pending_nudge: str | None = None
        self.final_answer: str | None = None
        self.invalid_action_attempts = 0
        self.delegate_calls = 0
        self._id_source = id_source if id_source is not None else itertools.count()

    def new_id(self) -> str:
        return str(next(self._id_source))

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


def stuckness_step(runner: LoopRunner) -> StepOutcome:
    result = assess(runner.session)
    if result.stuck:
        return "done"
    runner.pending_nudge = result.nudge
    return "continue"


def stream_step(runner: LoopRunner) -> StepOutcome:
    text = ""
    thinking = ""
    tool_calls: list[ToolCall] = []
    text_id: str | None = None
    thinking_id: str | None = None
    cancelled = False

    messages = render_messages(runner.session, runner.context_size)
    if runner.pending_nudge is not None:
        messages = [*messages, Message(role=Role.USER, content=runner.pending_nudge)]

    for event in runner.llm.stream(messages, runner.tools.specs()):
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
            case ToolCallDelta():
                pass
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


def _run_delegate(runner: LoopRunner, delegate: Delegate) -> tuple[str, bool]:
    runner.delegate_calls += 1
    child_session = runner.session.child(f"delegate/{runner.delegate_calls}")
    child_session.append(UserMessageRecorded(content=delegate.question))
    child_tools = ToolRegistry()
    register_delegate_actions(child_tools)
    child_runner = LoopRunner(
        runner.llm,
        child_tools,
        Bus(),
        child_session,
        runner.context_size,
        LoopConfig(steps=(stream_step, tool_call_step), max_steps=MAX_DELEGATE_STEPS),
    )
    child_runner.execute()
    if child_runner.final_answer is not None:
        return child_runner.final_answer, False
    return (
        f"delegate did not answer question within {MAX_DELEGATE_STEPS} steps: "
        f"{delegate.question!r}",
        True,
    )


def tool_call_step(runner: LoopRunner) -> StepOutcome:
    tool_calls = runner.pending_tool_calls
    runner.pending_tool_calls = []
    outcome: StepOutcome = "continue"
    for call in tool_calls:
        runner.bus.publish(ToolCallStarted(id=call.id, name=call.name, arguments=call.arguments))
        if call.name == "answer":
            try:
                answer = Answer.from_arguments(call.arguments)
                known = {fact.index for fact in facts(runner.session)}
                unknown = [index for index in answer.citations if index not in known]
                if unknown:
                    raise InvalidActionError(f"unknown fact citation(s): {unknown}")
                output = answer.content
                is_error = False
                runner.final_answer = answer.content
            except InvalidActionError as error:
                output = str(error)
                is_error = True
        elif call.name == "delegate":
            try:
                delegate = Delegate.from_arguments(call.arguments)
                output, is_error = _run_delegate(runner, delegate)
            except InvalidActionError as error:
                output = str(error)
                is_error = True
        else:
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
        if is_error:
            runner.invalid_action_attempts += 1
            if runner.invalid_action_attempts >= MAX_INVALID_ACTION_ATTEMPTS:
                outcome = "done"
        elif runner.final_answer is not None:
            outcome = "done"
    return outcome


DEFAULT_LOOP_CONFIG = LoopConfig(steps=(stuckness_step, stream_step, tool_call_step))
