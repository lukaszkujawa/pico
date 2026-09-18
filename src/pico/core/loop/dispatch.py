from pico.core.actions import (
    RUNNER_ACTIONS,
    ActionContext,
    ActionResult,
    AnswerOutcome,
    InvalidActionError,
)
from pico.core.errors import ToolError, UnknownToolError
from pico.core.events import AnswerSettled, ToolCallFinished, ToolCallStarted
from pico.core.ledger import BOOKKEEPING_TOOLS, facts
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.signals import Restrict
from pico.core.loop.state import Answered, LastWords
from pico.core.loop.subruns import spawn_delegate
from pico.core.search import SearchCancelled
from pico.core.tools import ToolRegistry
from pico.llm.errors import LLMError
from pico.llm.types import ToolCall
from pico.session import ToolCallRecorded

MAX_INVALID_ACTION_ATTEMPTS = 5


def _dispatch(
    context: ActionContext, tools: ToolRegistry, active: Restrict | None, call: ToolCall
) -> ActionResult:
    if active is not None and call.name not in active.allowed:
        raise InvalidActionError(f"{call.name} is not available — {active.rejection}")
    action = RUNNER_ACTIONS.get(call.name)
    if action is None:
        return tools.execute(call), False
    return action.execute(context, call.arguments)


def _failed(call: ToolCall, message: str) -> AnswerOutcome | tuple[str, bool]:
    if call.name != "answer":
        return message, True
    return AnswerOutcome(
        content="", result=message, is_error=True, accepted=False, reason=message, verify=None
    )


def _record(
    context: ActionContext, call: ToolCall, result: AnswerOutcome | tuple[str, bool]
) -> bool:
    if isinstance(result, AnswerOutcome):
        answer, output, is_error = result, result.result, result.is_error
    else:
        answer, (output, is_error) = None, result
    context.session.append(
        ToolCallRecorded(name=call.name, arguments=call.arguments, result=output, is_error=is_error)
    )
    if answer is not None:
        context.bus.publish(
            AnswerSettled(
                id=context.pane_id,
                content=answer.content,
                accepted=answer.accepted,
                reason=answer.reason,
                verify=answer.verify,
            )
        )
        return is_error
    fact_id = None
    if not is_error and call.name not in ("answer", "delegate", *BOOKKEEPING_TOOLS):
        fact_id = facts(context.session)[-1].id
    context.bus.publish(
        ToolCallFinished(
            id=context.pane_id, tool_call=call, result=output, is_error=is_error, fact_id=fact_id
        )
    )
    return is_error


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
        context = ActionContext(
            session=runner.session,
            llm=runner.llm,
            bus=runner.bus,
            pane_id=pane_id,
            cancel=runner.cancel,
            context_size=runner.context_size,
            chars_per_token=runner.chars_per_token,
            result_shape=runner.result_shape,
            spawn=lambda delegate: spawn_delegate(runner, delegate),
        )
        invalid = False
        result: ActionResult
        try:
            result = _dispatch(context, runner.tools, runner.active_restriction, call)
            if context.final_answer is not None:
                cause = runner.state.cause if isinstance(runner.state, LastWords) else None
                runner.state = Answered(context.final_answer, cause)
        except SearchCancelled:
            return "cancelled"
        except (InvalidActionError, UnknownToolError) as error:
            result = _failed(call, str(error))
            invalid = True
        except (ToolError, LLMError) as error:
            result = _failed(call, str(error))
        if result is None:
            return "cancelled"
        is_error = _record(context, call, result)
        if invalid:
            runner.dispatch.invalid_action_attempts += 1
            attempts = runner.dispatch.invalid_action_attempts
            if attempts >= MAX_INVALID_ACTION_ATTEMPTS:
                runner.fail(f"run stopped: {attempts} invalid actions in a row")
                outcome = "done"
        else:
            runner.dispatch.invalid_action_attempts = 0
            if not is_error and isinstance(runner.state, Answered):
                outcome = "done"
    return "done" if isinstance(runner.state, LastWords) else outcome
