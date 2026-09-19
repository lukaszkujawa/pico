from collections.abc import Mapping

from pico.core.actions.arguments import InvalidActionError, require
from pico.core.actions.context import ActionContext, ActionResult, RunnerAction
from pico.core.ledger import facts
from pico.core.search import search
from pico.core.tools import Tool, ToolError
from pico.llm.client import LLMError
from pico.llm.types import ToolSpec
from pico.session import Session

NOTE_SPEC = ToolSpec(
    name="note",
    description=(
        "Record an important finding, conclusion, or decision as a fact. "
        "Older messages fall out of your context, but facts are kept: "
        "noted findings stay in the fact index and can be recovered with "
        "read_fact or found again with search_facts."
    ),
    parameters={
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    },
)


def note_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        content = require(arguments, "content", str)
        if not content.strip():
            raise InvalidActionError("field 'content' must not be empty")
        return content

    return Tool(spec=NOTE_SPEC, execute=execute)


READ_FACT_SPEC = ToolSpec(
    name="read_fact",
    description=(
        "Recover the full content of a fact that was truncated to a handle, "
        "given the fact id shown in the handle."
    ),
    parameters={
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    },
)


def fact_recall_tool(session: Session) -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        fact_id = require(arguments, "id", int)
        for fact in facts(session):
            if fact.id == fact_id:
                return fact.content
        raise ToolError(f"no fact with id {fact_id}")

    return Tool(spec=READ_FACT_SPEC, execute=execute)


SEARCH_SPEC = ToolSpec(
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
)


def run_search(context: ActionContext, arguments: Mapping[str, object]) -> ActionResult:
    query = require(arguments, "query", str)
    if not query.strip():
        raise InvalidActionError("field 'query' must not be empty")
    try:
        output = search(
            context.llm,
            context.session,
            query,
            context.context_size,
            context.chars_per_token,
            context.cancel,
        )
    except LLMError as error:
        return f"search failed: {error}", True
    return output, False


SEARCH_ACTION = RunnerAction(spec=SEARCH_SPEC, execute=run_search)
