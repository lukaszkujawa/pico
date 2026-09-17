import itertools
import json
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    Answer,
    Delegate,
    InvalidActionError,
    ResultShape,
    Shell,
    register_actions,
    require,
)
from pico.core.bus import Bus
from pico.core.context import (
    RECENT_UNITS,
    SYSTEM_PROMPT,
    compile_context,
    estimate_tokens,
    message_text,
    message_tokens,
    prompt_budget,
    transcript_units,
)
from pico.core.errors import ToolError, UnknownToolError
from pico.core.events import (
    AnswerSettled,
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
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallResultDelta,
    ToolCallStarted,
)
from pico.core.ledger import BOOKKEEPING_TOOLS, Plan, facts, plan, render_plan
from pico.core.search import SearchCancelled, search
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
    PlanStepCompleted,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
)

StepOutcome = Literal["continue", "done", "cancelled"]

Step = Callable[["LoopRunner"], StepOutcome]

MAX_INVALID_ACTION_ATTEMPTS = 5
MAX_DELEGATE_STEPS = 10
MAX_STEP_STEPS = 30
MAX_RUN_STEPS = 100
MAX_STEP_ATTEMPTS = 2
BUDGET_WIND_DOWN_FRACTION = 0.8
MAX_ACTIONLESS_GENERATIONS = 3
DECISION_GRACE = 3
MAX_CROSSROADS = 2

LAST_WORDS_NUDGE = (
    "this run is ending now — {cause}. this is your final generation and answer is the "
    "only tool you have left. answer with what you have found so far, citing the facts "
    "that support it, and say plainly what is still unresolved."
)
NO_ACTION_NUDGE = (
    "you wrote text but took no action, and your plan has unfinished steps "
    "— call a tool to continue, or finish with answer"
)
DECISION_NUDGE = (
    "decision required — {cause}. either set_plan to hand the remaining work to fresh "
    "agents, or finish with answer. say which one and why, then do it."
)
CONTEXT_PRESSURE_CAUSE = (
    f"your context has passed {RECENT_UNITS} exchanges, so the earliest ones are now "
    "falling out of it"
)
NARRATION_CAUSE = "you wrote text but took no action, and you have no plan running"
CROSSROADS_ACTIONS = ("set_plan", "answer", "note")
UNVERIFIED_PREFIX = (
    "[unverified — the run ended without a final answer; this is its last narration, "
    "with no citations and no verification]"
)
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
        result_shape: ResultShape | None = None,
        depth: int = 0,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self.session = session
        self.context_size = context_size
        self.config = config
        self.cancel = cancel if cancel is not None else threading.Event()
        self.result_shape = result_shape
        self.depth = depth
        self.error: str | None = None
        self.pending_tool_calls: list[ToolCall] = []
        self.tool_call_pane_ids: dict[str, str] = {}
        self.pending_nudge: str | None = None
        self.chars_per_token = DEFAULT_CHARS_PER_TOKEN
        self.final_answer: str | None = None
        self.invalid_action_attempts = 0
        self.actionless_generations = 0
        self.step_attempts: dict[tuple[tuple[str, ...], int], int] = {}
        self.dying_of: str | None = None
        self.last_words: bool = False
        self.demanded: set[str] = set()
        self.demanded_at: int | None = None
        self.crossroads = False
        self.crossroads_generations = 0
        self.last_narration: str | None = None
        self.iterations = 0
        self._id_source = id_source if id_source is not None else itertools.count()

    def new_id(self) -> str:
        return str(next(self._id_source))

    def fail(self, message: str) -> None:
        self.error = message
        self.bus.publish(ErrorOccurred(message=message))

    def execute(self) -> None:
        self.bus.publish(RunStarted())
        try:
            max_steps = self.config.max_steps
            while max_steps is None or self.iterations <= max_steps:
                if max_steps is not None and self.iterations == max_steps:
                    self.dying_of = f"the generation budget of {max_steps} is spent"
                self.iterations += 1
                outcome = self._run_iteration()
                if outcome == "cancelled":
                    self.bus.publish(RunCancelled())
                    return
                if outcome == "done":
                    break
            if self.last_words and self.final_answer is None:
                self.fail(f"run stopped: {self.dying_of}")
        except Exception as error:
            self.fail(str(error))
        self.bus.publish(RunFinished(error=self.error))

    def _run_iteration(self) -> StepOutcome:
        for step in self.config.steps:
            if self.cancel.is_set():
                return "cancelled"
            outcome = step(self)
            if outcome != "continue":
                return outcome
        return "continue"


def stuckness_step(runner: LoopRunner) -> StepOutcome:
    if runner.dying_of is not None:
        return "continue"
    result = assess(runner.session)
    if result.stuck:
        runner.dying_of = f"you are stuck: {result.reason}"
        return "continue"
    if result.nudge is not None:
        runner.pending_nudge = result.nudge
    return "continue"


def _winding_down(runner: LoopRunner) -> bool:
    max_steps = runner.config.max_steps
    if max_steps is None or runner.depth > 0 or runner.dying_of is not None:
        return False
    return runner.iterations >= int(max_steps * BUDGET_WIND_DOWN_FRACTION)


def budget_step(runner: LoopRunner) -> StepOutcome:
    if not _winding_down(runner):
        return "continue"
    assert runner.config.max_steps is not None
    remaining = runner.config.max_steps - runner.iterations
    runner.pending_nudge = (
        f"the generation budget is nearly spent — {remaining} generations remain. "
        "stop exploring, complete or prune the plan, and finish with answer using "
        "the facts you have gathered"
    )
    return "continue"


def _undecided(runner: LoopRunner) -> bool:
    if runner.dying_of is not None or runner.final_answer is not None:
        return False
    current = plan(runner.session)
    return current is None or all(step.done for step in current.steps)


def _pressure(runner: LoopRunner) -> tuple[str, str] | None:
    if transcript_units(runner.session.messages()) > RECENT_UNITS:
        return "context", CONTEXT_PRESSURE_CAUSE
    if _winding_down(runner):
        assert runner.config.max_steps is not None
        remaining = runner.config.max_steps - runner.iterations
        return "budget", f"only {remaining} generations remain of your budget"
    return None


def decision_step(runner: LoopRunner) -> StepOutcome:
    if runner.crossroads:
        runner.crossroads = False
        runner.crossroads_generations += 1
    if not _undecided(runner):
        runner.demanded.clear()
        runner.demanded_at = None
        runner.crossroads_generations = 0
        return "continue"
    if runner.crossroads_generations >= MAX_CROSSROADS:
        return _degraded_ending(runner)
    if runner.demanded_at is not None:
        ignored_for = runner.iterations - runner.demanded_at
        runner.crossroads = runner.crossroads_generations > 0 or ignored_for > DECISION_GRACE
        return "continue"
    signal = _pressure(runner)
    if signal is not None:
        _demand(runner, *signal)
    return "continue"


def _demand(runner: LoopRunner, name: str, cause: str) -> None:
    if name in runner.demanded:
        return
    runner.demanded.add(name)
    runner.demanded_at = runner.iterations
    runner.pending_nudge = DECISION_NUDGE.format(cause=cause)


def _degraded_ending(runner: LoopRunner) -> StepOutcome:
    narration = runner.last_narration
    if narration is None:
        runner.dying_of = (
            f"{MAX_CROSSROADS} decision points passed with neither a plan nor an answer"
        )
        return "continue"
    runner.final_answer = f"{UNVERIFIED_PREFIX}\n\n{narration}"
    runner.bus.publish(
        AnswerSettled(
            id=runner.new_id(), content=runner.final_answer, accepted=True, reason=None, verify=None
        )
    )
    return "done"


def specs_text(specs: list[ToolSpec]) -> str:
    return json.dumps(
        [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in specs
        ],
        sort_keys=True,
    )


@dataclass(frozen=True)
class Restriction:
    allowed: tuple[str, ...]
    nudge: str


def _restriction(runner: LoopRunner) -> Restriction | None:
    runner.last_words = runner.dying_of is not None
    if runner.dying_of is not None:
        return Restriction(
            allowed=("answer",), nudge=LAST_WORDS_NUDGE.format(cause=runner.dying_of)
        )
    if runner.crossroads:
        signal = _pressure(runner)
        cause = CONTEXT_PRESSURE_CAUSE if signal is None else signal[1]
        return Restriction(
            allowed=CROSSROADS_ACTIONS,
            nudge=runner.pending_nudge or DECISION_NUDGE.format(cause=cause),
        )
    return None


def _reconcile(runner: LoopRunner, sent_chars: int, prompt_tokens: int) -> None:
    observed = sent_chars / prompt_tokens
    runner.chars_per_token = min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, observed))


def stream_step(runner: LoopRunner) -> StepOutcome:
    text = ""
    thinking = ""
    tool_calls: list[ToolCall] = []
    text_id: str | None = None
    thinking_id: str | None = None
    cancelled = False
    runner.tool_call_pane_ids = {}

    restriction = _restriction(runner)
    if restriction is None:
        specs = vocabulary(runner.tools, runner.depth)
    else:
        specs = restricted_vocabulary(runner.tools, restriction.allowed)
        runner.pending_nudge = restriction.nudge
    preamble = [Message(role=Role.SYSTEM, content=SYSTEM_PROMPT)]
    postamble = (
        []
        if runner.pending_nudge is None
        else [Message(role=Role.USER, content=runner.pending_nudge)]
    )
    runner.pending_nudge = None
    overhead_text = specs_text(specs) + "".join(
        message_text(message) for message in [*preamble, *postamble]
    )
    overhead_tokens = estimate_tokens(overhead_text, runner.chars_per_token)
    conversation = compile_context(
        runner.session,
        runner.context_size,
        overhead_tokens,
        runner.chars_per_token,
        runner.depth < MAX_DELEGATE_DEPTH,
    )
    messages = [*preamble, *conversation, *postamble]
    estimated = overhead_tokens + sum(
        message_tokens(message, runner.chars_per_token) for message in conversation
    )
    sent_chars = len(overhead_text) + sum(len(message_text(message)) for message in conversation)
    budget = prompt_budget(runner.context_size)
    if estimated > budget:
        runner.bus.publish(BudgetExceeded(estimated=estimated, budget=budget))

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
                        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                    )
                )
                if prompt_tokens:
                    _reconcile(runner, sent_chars, prompt_tokens)

    if thinking_id is not None:
        runner.bus.publish(AssistantThinkingFinished(id=thinking_id))
    if text_id is not None:
        runner.bus.publish(AssistantTextFinished(id=text_id))

    if cancelled:
        return "cancelled"

    if text or thinking:
        runner.session.append(AssistantMessageRecorded(content=text, thinking=thinking))

    if text.strip():
        runner.last_narration = text

    if not tool_calls:
        if runner.last_words:
            return "done"
        if _undecided(runner):
            runner.actionless_generations = 0
            _demand(runner, "narration", NARRATION_CAUSE)
            return "continue"
        runner.actionless_generations += 1
        if runner.actionless_generations >= MAX_ACTIONLESS_GENERATIONS:
            runner.fail(
                f"run stopped: {MAX_ACTIONLESS_GENERATIONS} generations without a tool call "
                "while the plan has unfinished steps"
            )
            return "done"
        runner.pending_nudge = NO_ACTION_NUDGE
        return "continue"

    runner.actionless_generations = 0
    runner.pending_tool_calls = tool_calls
    return "continue"


def _run_child(
    runner: LoopRunner,
    suffix: str,
    prompt: str,
    max_steps: int,
    shape: ResultShape | None = None,
) -> LoopRunner:
    child_session = runner.session.child(suffix)
    child_session.append(UserMessageRecorded(content=prompt))
    child_tools = ToolRegistry()
    register_actions(child_tools, child_session, depth=runner.depth + 1)
    child_runner = LoopRunner(
        runner.llm,
        child_tools,
        Bus(),
        child_session,
        runner.context_size,
        LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=max_steps),
        cancel=runner.cancel,
        result_shape=shape,
        depth=runner.depth + 1,
    )
    child_runner.execute()
    return child_runner


def _run_delegate(runner: LoopRunner, delegate: Delegate) -> tuple[str, bool]:
    if runner.depth >= MAX_DELEGATE_DEPTH:
        raise InvalidActionError("delegate is not available at this depth")
    prompt = "" if delegate.shape is None else delegate.shape.prompt()
    child_runner = _run_child(
        runner,
        f"delegate/{runner.session.next_seq()}",
        delegate.question + prompt,
        MAX_DELEGATE_STEPS,
        delegate.shape,
    )
    if child_runner.final_answer is not None:
        return child_runner.final_answer, False
    if child_runner.error is not None:
        return f"delegate failed: {child_runner.error}", True
    return (
        f"delegate did not answer question within {MAX_DELEGATE_STEPS} steps: "
        f"{delegate.question!r}",
        True,
    )


def root_task(session: Session) -> str:
    return next(
        (event.content for event in session.events() if isinstance(event, UserMessageRecorded)), ""
    )


def completed_step_results(session: Session) -> list[tuple[str, str]]:
    return [
        (str(event.arguments.get("step", "")), event.result)
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step" and not event.is_error
    ]


def compose_handoff(session: Session, current: Plan, index: int) -> str:
    sections = [
        f"You are working on this task:\n{root_task(session)}",
        f"It has been broken into steps:\n{render_plan(current)}",
    ]
    results = completed_step_results(session)
    if results:
        finished = "\n\n".join(f'Result of "{text}":\n{result}' for text, result in results)
        sections.append(f"Earlier steps produced these results:\n\n{finished}")
    sections.append(
        f"Your step is step {index}: {current.steps[index].text}\n"
        "Do only this step, then answer with its result. Your answer is all that survives "
        "your context, so state the findings the later steps need, not just that you are done."
    )
    return "\n\n".join(sections)


def _first_unfinished(current: Plan | None) -> int | None:
    if current is None:
        return None
    return next((index for index, step in enumerate(current.steps) if not step.done), None)


def _step_result(child: LoopRunner, index: int, text: str) -> tuple[str, bool]:
    if child.final_answer is not None:
        if not child.last_words:
            return child.final_answer, False
        return f"partial — {child.dying_of}:\n{child.final_answer}", False
    reason = child.error if child.error is not None else f"no answer within {MAX_STEP_STEPS} steps"
    return f'step {index} failed — {reason}\nstep was: "{text}"', True


def step_orchestration_step(runner: LoopRunner) -> StepOutcome:
    if runner.depth >= MAX_DELEGATE_DEPTH or runner.dying_of is not None:
        return "continue"
    current = plan(runner.session)
    index = _first_unfinished(current)
    if current is None or index is None:
        runner.step_attempts = {}
        return "continue"
    signature = (tuple(step.text for step in current.steps), index)
    attempts = runner.step_attempts.get(signature, 0)
    if attempts >= MAX_STEP_ATTEMPTS:
        runner.dying_of = f'step {index} failed twice: "{current.steps[index].text}"'
        return "continue"
    runner.step_attempts[signature] = attempts + 1

    text = current.steps[index].text
    pane_id = runner.new_id()
    arguments: Mapping[str, object] = {"step": text}
    runner.bus.publish(ToolCallStarted(id=pane_id, name="step", arguments=arguments))
    child = _run_child(
        runner,
        f"step/{runner.session.next_seq()}",
        compose_handoff(runner.session, current, index),
        MAX_STEP_STEPS,
    )
    if runner.cancel.is_set():
        return "cancelled"
    result, is_error = _step_result(child, index, text)
    runner.session.append(
        ToolCallRecorded(name="step", arguments=arguments, result=result, is_error=is_error)
    )
    if not is_error:
        runner.session.append(PlanStepCompleted(index=index))
    fact_id = facts(runner.session)[-1].id if not is_error else None
    runner.bus.publish(
        ToolCallFinished(
            id=pane_id,
            tool_call=ToolCall(id=pane_id, name="step", arguments=arguments),
            result=result,
            is_error=is_error,
            fact_id=fact_id,
        )
    )
    return "continue"


@dataclass(frozen=True)
class AnswerOutcome:
    content: str
    result: str
    is_error: bool
    accepted: bool
    reason: str | None
    verify: str | None


def _rejected_answer(answer: Answer, reason: str) -> AnswerOutcome:
    return AnswerOutcome(
        content=answer.content,
        result=f"answer rejected — {reason}",
        is_error=True,
        accepted=False,
        reason=reason,
        verify=answer.verify,
    )


def _verified_answer(runner: LoopRunner, answer: Answer) -> AnswerOutcome:
    problem = None if runner.result_shape is None else runner.result_shape.check(answer.content)
    if problem is not None:
        return _rejected_answer(answer, problem)
    if answer.verify is None:
        runner.final_answer = answer.content
        return AnswerOutcome(
            content=answer.content,
            result=answer.content,
            is_error=False,
            accepted=True,
            reason=None,
            verify=None,
        )
    code, output = Shell(command=answer.verify).run()
    if code != 0:
        return _rejected_answer(answer, f"verification failed (exit {code}):\n{output}")
    runner.final_answer = answer.content
    return AnswerOutcome(
        content=answer.content,
        result=f"{answer.content}\n\nverified: {answer.verify}",
        is_error=False,
        accepted=True,
        reason=None,
        verify=answer.verify,
    )


def _run_shell(runner: LoopRunner, pane_id: str, shell: Shell) -> tuple[str, bool]:
    def on_chunk(chunk: str) -> None:
        runner.bus.publish(ToolCallResultDelta(id=pane_id, text=chunk))

    code, output = shell.run(on_chunk=on_chunk)
    if code != 0:
        return f"exit code {code}\n{output}", True
    return output, False


ActionResult = AnswerOutcome | tuple[str, bool] | None


@dataclass(frozen=True)
class RunnerAction:
    spec: ToolSpec
    execute: Callable[[LoopRunner, str, Mapping[str, object]], ActionResult]


def _shell_action(
    runner: LoopRunner, pane_id: str, arguments: Mapping[str, object]
) -> ActionResult:
    return _run_shell(runner, pane_id, Shell.from_arguments(arguments))


def _answer_action(
    runner: LoopRunner, pane_id: str, arguments: Mapping[str, object]
) -> ActionResult:
    answer = Answer.from_arguments(arguments)
    known = {fact.id for fact in facts(runner.session)}
    unknown = [citation for citation in answer.citations if citation not in known]
    if unknown:
        raise InvalidActionError(f"unknown fact citation(s): {unknown}")
    return _verified_answer(runner, answer)


def _search_action(
    runner: LoopRunner, pane_id: str, arguments: Mapping[str, object]
) -> ActionResult:
    query = require(arguments, "query", str)
    if not query.strip():
        raise InvalidActionError("field 'query' must not be empty")
    try:
        output = search(
            runner.llm,
            runner.session,
            query,
            runner.context_size,
            runner.chars_per_token,
            runner.cancel,
        )
    except LLMError as error:
        return f"search failed: {error}", True
    return output, False


def _delegate_action(
    runner: LoopRunner, pane_id: str, arguments: Mapping[str, object]
) -> ActionResult:
    result = _run_delegate(runner, Delegate.from_arguments(arguments))
    if runner.cancel.is_set():
        return None
    return result


RUNNER_ACTIONS: dict[str, RunnerAction] = {
    action.spec.name: action
    for action in (
        RunnerAction(
            spec=ToolSpec(
                name="shell",
                description="Run a shell command and return its combined stdout and stderr.",
                parameters={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            ),
            execute=_shell_action,
        ),
        RunnerAction(
            spec=ToolSpec(
                name="answer",
                description=(
                    "Give the final answer to the user and end the run. "
                    "Whenever the task has a checkable outcome, pass verify: a shell command "
                    "that exits 0 exactly when your answer's claim is true. The runtime runs "
                    "it before accepting the answer and rejects the answer if it fails."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "citations": {"type": "array", "items": {"type": "integer"}},
                        "verify": {"type": "string"},
                    },
                    "required": ["content", "citations"],
                },
            ),
            execute=_answer_action,
        ),
        RunnerAction(
            spec=ToolSpec(
                name="search_facts",
                description=(
                    "Search all recorded facts (tool results and notes) by describing what you "
                    "are looking for in plain words. A sub-task reads every recorded fact and "
                    "judges relevance against your query, so keywords need not appear "
                    "literally. Returns the relevant fact ids, each with a reason; "
                    "read_fact(id) recovers any of them in full."
                ),
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            execute=_search_action,
        ),
        RunnerAction(
            spec=ToolSpec(
                name="delegate",
                description=(
                    "Spawn a sub-agent with its own fresh context to answer a single scoped "
                    "question and return its answer. It has the same tools as you: it can "
                    "explore with shell, work to its own plan, and delegate further. Use it "
                    "to keep large exploration out of your own context. Pass fields to "
                    "require a typed result: a mapping of field name to 'string', 'number', "
                    "or 'boolean'. The runtime then rejects any answer that is not a JSON "
                    "object with exactly those fields, so what comes back is "
                    "machine-readable."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "fields": {
                            "type": "object",
                            "additionalProperties": {
                                "type": "string",
                                "enum": ["string", "number", "boolean"],
                            },
                        },
                    },
                    "required": ["question"],
                },
            ),
            execute=_delegate_action,
        ),
    )
}


def vocabulary(tools: ToolRegistry, depth: int) -> list[ToolSpec]:
    specs = [*tools.specs(), *(action.spec for action in RUNNER_ACTIONS.values())]
    if depth >= MAX_DELEGATE_DEPTH:
        return [spec for spec in specs if spec.name != "delegate"]
    return specs


def restricted_vocabulary(tools: ToolRegistry, allowed: tuple[str, ...]) -> list[ToolSpec]:
    specs = {spec.name: spec for spec in vocabulary(tools, 0)}
    return [specs[name] for name in allowed if name in specs]


def _dispatch(runner: LoopRunner, pane_id: str, call: ToolCall) -> ActionResult:
    if runner.last_words and call.name != "answer":
        raise InvalidActionError(f"{call.name} is not available — answer is the only tool left")
    if runner.crossroads and call.name not in CROSSROADS_ACTIONS:
        raise InvalidActionError(
            f"{call.name} is not available — a decision is required first; "
            f"the tools you have are {', '.join(CROSSROADS_ACTIONS)}"
        )
    action = RUNNER_ACTIONS.get(call.name)
    if action is not None:
        return action.execute(runner, pane_id, call.arguments)
    return runner.tools.execute(call), False


def _failed(call: ToolCall, message: str) -> AnswerOutcome | tuple[str, bool]:
    if call.name != "answer":
        return message, True
    return AnswerOutcome(
        content="", result=message, is_error=True, accepted=False, reason=message, verify=None
    )


def tool_call_step(runner: LoopRunner) -> StepOutcome:
    tool_calls = runner.pending_tool_calls
    runner.pending_tool_calls = []
    outcome: StepOutcome = "continue"
    for call in tool_calls:
        if runner.cancel.is_set():
            return "cancelled"
        pane_id = runner.tool_call_pane_ids.get(call.id)
        if pane_id is None:
            pane_id = runner.new_id()
        runner.bus.publish(ToolCallStarted(id=pane_id, name=call.name, arguments=call.arguments))
        invalid = False
        result: ActionResult
        try:
            result = _dispatch(runner, pane_id, call)
        except SearchCancelled:
            return "cancelled"
        except (InvalidActionError, UnknownToolError) as error:
            result = _failed(call, str(error))
            invalid = True
        except (ToolError, LLMError) as error:
            result = _failed(call, str(error))
        if result is None:
            return "cancelled"
        if isinstance(result, AnswerOutcome):
            answer_outcome = result
            output, is_error = result.result, result.is_error
        else:
            answer_outcome = None
            output, is_error = result
        runner.session.append(
            ToolCallRecorded(
                name=call.name, arguments=call.arguments, result=output, is_error=is_error
            )
        )
        fact_id = None
        if not is_error and call.name not in ("answer", "delegate", *BOOKKEEPING_TOOLS):
            fact_id = facts(runner.session)[-1].id
        if answer_outcome is not None:
            runner.bus.publish(
                AnswerSettled(
                    id=pane_id,
                    content=answer_outcome.content,
                    accepted=answer_outcome.accepted,
                    reason=answer_outcome.reason,
                    verify=answer_outcome.verify,
                )
            )
        else:
            runner.bus.publish(
                ToolCallFinished(
                    id=pane_id, tool_call=call, result=output, is_error=is_error, fact_id=fact_id
                )
            )
        if invalid:
            runner.invalid_action_attempts += 1
            if runner.invalid_action_attempts >= MAX_INVALID_ACTION_ATTEMPTS:
                runner.fail(
                    f"run stopped: {runner.invalid_action_attempts} invalid actions in a row"
                )
                outcome = "done"
        else:
            runner.invalid_action_attempts = 0
            if not is_error and runner.final_answer is not None:
                outcome = "done"
    return "done" if runner.last_words else outcome


DEFAULT_LOOP_STEPS: tuple[Step, ...] = (
    stuckness_step,
    budget_step,
    decision_step,
    step_orchestration_step,
    stream_step,
    tool_call_step,
)
DEFAULT_LOOP_CONFIG = LoopConfig(steps=DEFAULT_LOOP_STEPS, max_steps=MAX_RUN_STEPS)
