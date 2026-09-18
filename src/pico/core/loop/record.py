from pico.core.bus import Bus
from pico.core.events import ToolCallFinished
from pico.core.ledger import BOOKKEEPING_TOOLS, facts
from pico.llm.types import ToolCall
from pico.session import Session, ToolCallRecorded

FACTLESS_TOOLS = frozenset({"answer", "delegate"}) | BOOKKEEPING_TOOLS


def finish_tool_call(
    session: Session, bus: Bus, pane_id: str, call: ToolCall, result: str, is_error: bool
) -> None:
    session.append(
        ToolCallRecorded(name=call.name, arguments=call.arguments, result=result, is_error=is_error)
    )
    fact_id = facts(session)[-1].id if not is_error and call.name not in FACTLESS_TOOLS else None
    bus.publish(
        ToolCallFinished(
            id=pane_id, tool_call=call, result=result, is_error=is_error, fact_id=fact_id
        )
    )
