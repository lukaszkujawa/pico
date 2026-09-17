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
from pico.core.loop.subruns import run_delegate
from pico.core.search import SearchCancelled
from pico.llm.errors import LLMError
from pico.llm.types import ToolCall
from pico.session import ToolCallRecorded

MAX_INVALID_ACTION_ATTEMPTS = 5


def _context(runner: LoopRunner, pane_id: str) -> ActionContext:
    return ActionContext(
        session=runner.session,
        llm=runner.llm,
        bus=runner.bus,
        pane_id=pane_id,
        cancel=runner.cancel,
        context_size=runner.context_size,
        chars_per_token=runner.chars_per_token,
        result_shape=runner.result_shape,
        spawn=lambda delegate: run_delegate(runner, delegate),
    )


def _dispatch(runner: LoopRunner, pane_id: str, call: ToolCall) -> ActionResult:
    active = runner.active_restriction
    if active is not None and call.name not in active.allowed:
        raise InvalidActionError(f"{call.name} is not available — {active.rejection}")
    action = RUNNER_ACTIONS.get(call.name)
    if action is None:
        return runner.tools.execute(call), False
    context = _context(runner, pane_id)
    result = action.execute(context, call.arguments)
    if context.final_answer is not None:
        runner.final_answer = context.final_answer
    return result


def _failed(call: ToolCall, message: str) -> AnswerOutcome | tuple[str, bool]:
    if call.name != "answer":
        return message, True
    return AnswerOutcome(
        content="", result=message, is_error=True, accepted=False, reason=message, verify=None
    )


def _record(
    runner: LoopRunner, pane_id: str, call: ToolCall, result: AnswerOutcome | tuple[str, bool]
) -> bool:
    if isinstance(result, AnswerOutcome):
        answer, output, is_error = result, result.result, result.is_error
    else:
        answer, (output, is_error) = None, result
    runner.session.append(
        ToolCallRecorded(name=call.name, arguments=call.arguments, result=output, is_error=is_error)
    )
    if answer is not None:
        runner.bus.publish(
            AnswerSettled(
                id=pane_id,
                content=answer.content,
                accepted=answer.accepted,
                reason=answer.reason,
                verify=answer.verify,
            )
        )
        return is_error
    fact_id = None
    if not is_error and call.name not in ("answer", "delegate", *BOOKKEEPING_TOOLS):
        fact_id = facts(runner.session)[-1].id
    runner.bus.publish(
        ToolCallFinished(
            id=pane_id, tool_call=call, result=output, is_error=is_error, fact_id=fact_id
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
        is_error = _record(runner, pane_id, call, result)
        if invalid:
            runner.dispatch.invalid_action_attempts += 1
            attempts = runner.dispatch.invalid_action_attempts
            if attempts >= MAX_INVALID_ACTION_ATTEMPTS:
                runner.fail(f"run stopped: {attempts} invalid actions in a row")
                outcome = "done"
        else:
            runner.dispatch.invalid_action_attempts = 0
            if not is_error and runner.final_answer is not None:
                outcome = "done"
    return "done" if runner.last_words else outcome
