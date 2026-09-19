from pico.core.actions import (
    RUNNER_ACTIONS,
    ActionContext,
    ActionResult,
    AnswerOutcome,
    InvalidActionError,
    Outcome,
)
from pico.core.events import AnswerSettled, ToolCallStarted
from pico.core.loop.record import finish_tool_call
from pico.core.loop.runner import LoopRunner, StepOutcome
from pico.core.loop.state import Answered, LastWords, Restrict
from pico.core.loop.subruns import spawn_delegate
from pico.core.search import SearchCancelled
from pico.core.tools import ToolError, ToolRegistry, UnknownToolError
from pico.llm.client import LLMError
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
        return Outcome(tools.execute(call))
    return action.execute(context, call.arguments)


def _failed(call: ToolCall, message: str) -> AnswerOutcome | Outcome:
    if call.name != "answer":
        return Outcome(message, is_error=True)
    return AnswerOutcome(
        content="", result=message, is_error=True, accepted=False, reason=message, verify=None
    )


def _record(context: ActionContext, call: ToolCall, result: AnswerOutcome | Outcome) -> bool:
    if isinstance(result, Outcome):
        finish_tool_call(context.session, context.bus, context.pane_id, call, result)
        return result.is_error
    context.session.append(
        ToolCallRecorded(
            name=call.name,
            arguments=call.arguments,
            result=result.result,
            is_error=result.is_error,
        )
    )
    context.bus.publish(
        AnswerSettled(
            id=context.pane_id,
            content=result.content,
            accepted=result.accepted,
            reason=result.reason,
            verify=result.verify,
        )
    )
    return result.is_error


def _action_context(runner: LoopRunner, pane_id: str) -> ActionContext:
    return ActionContext(
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


def _execute(
    runner: LoopRunner, context: ActionContext, call: ToolCall
) -> tuple[ActionResult, bool]:
    try:
        return _dispatch(context, runner.tools, runner.active_restriction, call), False
    except SearchCancelled:
        return None, False
    except (InvalidActionError, UnknownToolError) as error:
        return _failed(call, str(error)), True
    except (ToolError, LLMError) as error:
        return _failed(call, str(error)), False


def _too_many_invalid_actions(runner: LoopRunner, invalid: bool) -> bool:
    if not invalid:
        runner.dispatch.invalid_action_attempts = 0
        return False
    runner.dispatch.invalid_action_attempts += 1
    attempts = runner.dispatch.invalid_action_attempts
    if attempts < MAX_INVALID_ACTION_ATTEMPTS:
        return False
    runner.fail(f"run stopped: {attempts} invalid actions in a row")
    return True


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
        context = _action_context(runner, pane_id)
        result, invalid = _execute(runner, context, call)
        if context.final_answer is not None:
            cause = runner.state.cause if isinstance(runner.state, LastWords) else None
            runner.state = Answered(context.final_answer, cause)
        if result is None:
            return "cancelled"
        is_error = _record(context, call, result)
        stopped = _too_many_invalid_actions(runner, invalid)
        if stopped or (not is_error and isinstance(runner.state, Answered)):
            outcome = "done"
    return "done" if isinstance(runner.state, LastWords) else outcome
