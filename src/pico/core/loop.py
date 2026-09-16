import itertools
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from pico.core.actions import (
    Answer,
    Delegate,
    InvalidActionError,
    Shell,
    register_delegate_actions,
)
from pico.core.bus import Bus
from pico.core.context import (
    SYSTEM_PROMPT,
    estimate_tokens,
    message_text,
    prompt_budget,
    render_messages,
)
from pico.core.errors import ToolError, UnknownToolError
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BudgetExceeded,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.ledger import Plan, facts, plan, render_plan
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
    ToolSpec,
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
DEFAULT_CHARS_PER_TOKEN = 4.0
MIN_CHARS_PER_TOKEN = 2.0
MAX_CHARS_PER_TOKEN = 6.0


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
        can_verify: bool = True,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self.session = session
        self.context_size = context_size
        self.config = config
        self.cancel = cancel if cancel is not None else threading.Event()
        self.can_verify = can_verify
        self.pending_tool_calls: list[ToolCall] = []
        self.pending_nudge: str | None = None
        self.chars_per_token = DEFAULT_CHARS_PER_TOKEN
        self.final_answer: str | None = None
        self.invalid_action_attempts = 0
        self.delegate_calls = 0
        self.iterations = 0
        self._id_source = id_source if id_source is not None else itertools.count()

    def new_id(self) -> str:
        return str(next(self._id_source))

    def execute(self) -> None:
        self.bus.publish(RunStarted())
        try:
            while self.config.max_steps is None or self.iterations < self.config.max_steps:
                self.iterations += 1
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


def _plan_message(current: Plan) -> Message:
    return Message(
        role=Role.USER,
        content=(
            f"Your current plan:\n{render_plan(current)}\n"
            "Keep it current with set_plan and complete_step."
        ),
    )


def specs_text(specs: list[ToolSpec]) -> str:
    return json.dumps(
        [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in specs
        ],
        sort_keys=True,
    )


def _reconcile(runner: LoopRunner, estimated: int, sent_chars: int, prompt_tokens: int) -> None:
    observed = sent_chars / prompt_tokens
    runner.chars_per_token = min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, observed))
    budget = prompt_budget(runner.context_size)
    if prompt_tokens > budget:
        runner.bus.publish(BudgetExceeded(estimated=estimated, actual=prompt_tokens, budget=budget))


def stream_step(runner: LoopRunner) -> StepOutcome:
    text = ""
    thinking = ""
    tool_calls: list[ToolCall] = []
    text_id: str | None = None
    thinking_id: str | None = None
    cancelled = False

    current_plan = plan(runner.session)
    specs = runner.tools.specs()
    preamble = [
        Message(role=Role.SYSTEM, content=SYSTEM_PROMPT),
        *([] if current_plan is None else [_plan_message(current_plan)]),
    ]
    postamble = (
        []
        if runner.pending_nudge is None
        else [Message(role=Role.USER, content=runner.pending_nudge)]
    )
    overhead_text = specs_text(specs) + "".join(
        message_text(message) for message in [*preamble, *postamble]
    )
    overhead_tokens = estimate_tokens(overhead_text, runner.chars_per_token)
    conversation = render_messages(
        runner.session, runner.context_size, overhead_tokens, runner.chars_per_token
    )
    messages = [*preamble, *conversation, *postamble]
    estimated = overhead_tokens + sum(
        estimate_tokens(message_text(message), runner.chars_per_token) for message in conversation
    )
    sent_chars = len(overhead_text) + sum(len(message_text(message)) for message in conversation)

    for event in runner.llm.stream(messages, specs):
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
            case GenerationComplete(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            ):
                runner.bus.publish(
                    GenerationCompleted(
                        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                    )
                )
                if prompt_tokens:
                    _reconcile(runner, estimated, sent_chars, prompt_tokens)

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
    register_delegate_actions(child_tools, child_session)
    child_runner = LoopRunner(
        runner.llm,
        child_tools,
        Bus(),
        child_session,
        runner.context_size,
        LoopConfig(steps=(stream_step, tool_call_step), max_steps=MAX_DELEGATE_STEPS),
        can_verify=False,
    )
    child_runner.execute()
    if child_runner.final_answer is not None:
        return child_runner.final_answer, False
    return (
        f"delegate did not answer question within {MAX_DELEGATE_STEPS} steps: "
        f"{delegate.question!r}",
        True,
    )


def _verified_answer(runner: LoopRunner, answer: Answer) -> tuple[str, bool]:
    if answer.verify is None:
        runner.final_answer = answer.content
        return answer.content, False
    code, output = Shell(command=answer.verify).run()
    if code != 0:
        return f"answer rejected — verification failed (exit {code}):\n{output}", True
    runner.final_answer = answer.content
    return f"{answer.content}\n\nverified: {answer.verify}", False


def tool_call_step(runner: LoopRunner) -> StepOutcome:
    tool_calls = runner.pending_tool_calls
    runner.pending_tool_calls = []
    outcome: StepOutcome = "continue"
    for call in tool_calls:
        runner.bus.publish(ToolCallStarted(id=call.id, name=call.name, arguments=call.arguments))
        invalid = False
        if call.name == "answer":
            try:
                answer = Answer.from_arguments(call.arguments)
                known = {fact.id for fact in facts(runner.session)}
                unknown = [citation for citation in answer.citations if citation not in known]
                if unknown:
                    raise InvalidActionError(f"unknown fact citation(s): {unknown}")
                if answer.verify is not None and not runner.can_verify:
                    raise InvalidActionError(
                        "a delegate may not use 'verify'; answer from what you have read"
                    )
                output, is_error = _verified_answer(runner, answer)
            except InvalidActionError as error:
                output = str(error)
                is_error = True
                invalid = True
            except ToolError as error:
                output = str(error)
                is_error = True
        elif call.name == "delegate":
            try:
                delegate = Delegate.from_arguments(call.arguments)
                output, is_error = _run_delegate(runner, delegate)
            except InvalidActionError as error:
                output = str(error)
                is_error = True
                invalid = True
        else:
            try:
                output = runner.tools.execute(call)
                is_error = False
            except ToolError as error:
                output = str(error)
                is_error = True
            except UnknownToolError as error:
                output = str(error)
                is_error = True
                invalid = True
        runner.bus.publish(
            ToolCallFinished(id=call.id, tool_call=call, result=output, is_error=is_error)
        )
        runner.session.append(
            ToolCallRecorded(
                name=call.name, arguments=call.arguments, result=output, is_error=is_error
            )
        )
        if invalid:
            runner.invalid_action_attempts += 1
            if runner.invalid_action_attempts >= MAX_INVALID_ACTION_ATTEMPTS:
                outcome = "done"
        elif not is_error and runner.final_answer is not None:
            outcome = "done"
    return outcome


DEFAULT_LOOP_CONFIG = LoopConfig(steps=(stuckness_step, stream_step, tool_call_step))
