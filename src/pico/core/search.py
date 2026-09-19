import re
import threading
from collections.abc import Callable, Iterator

from pico.core.ledger import Fact, fact_index, facts, render_call
from pico.llm.budget import prompt_budget
from pico.llm.client import LLMClient
from pico.llm.types import Message, Role, TextDelta
from pico.session import Session

SCAN_PREVIEW_CHARS = 200
REFINE_CONTENT_CHARS = 2000
MIN_PAGE_CHARS = 1000

_INSTRUCTIONS = "You are a search index for an agent's fact ledger."

_SCAN_TASK = (
    "Reply with the ids of the facts that look relevant to the query, "
    "comma-separated, or none if no fact looks relevant. Reply with nothing else."
)

_REFINE_TASK = (
    "Reply with the ids of the facts that actually answer or bear on the query, "
    "comma-separated, or none if none of them do. Reply with nothing else."
)

_REDUCE_TASK = (
    "Reply with one line per relevant fact, most relevant first, "
    "formatted as [id] why it is relevant. Reply with nothing else."
)

_RECOVERY_HINT = "Call read_fact(id) to recover any fact in full."


class SearchCancelled(Exception):
    pass


def _scan_line(fact: Fact) -> str:
    flat = " ".join(fact.content.split())
    signature = render_call(fact.source, fact.arguments)
    return f"[{fact.id}] {signature}: {flat[:SCAN_PREVIEW_CHARS]}"


def _refine_line(fact: Fact) -> str:
    signature = render_call(fact.source, fact.arguments)
    return f"[{fact.id}] {signature}: {fact.content[:REFINE_CONTENT_CHARS]}"


def _pages(lines: list[str], page_chars: int) -> Iterator[str]:
    page: list[str] = []
    size = 0
    for line in lines:
        if page and size + len(line) > page_chars:
            yield "\n".join(page)
            page, size = [], 0
        page.append(line)
        size += len(line) + 1
    if page:
        yield "\n".join(page)


def _ask(llm: LLMClient, cancel: threading.Event, task: str, query: str, page: str) -> str:
    if cancel.is_set():
        raise SearchCancelled
    content = f"{_INSTRUCTIONS}\nQuery: {query}\n\nFacts:\n{page}\n\n{task}"
    reply = "".join(
        event.text
        for event in llm.stream([Message(role=Role.USER, content=content)], [])
        if isinstance(event, TextDelta)
    )
    if cancel.is_set():
        raise SearchCancelled
    return reply


_LEADING_IDS = re.compile(r"^[\[\s]*\d+(?:[\]\s,]+\d+)*")


def _selected(reply: str, candidates: list[Fact]) -> list[Fact]:
    by_id = {fact.id: fact for fact in candidates}
    chosen: list[Fact] = []
    for line in reply.splitlines():
        head = _LEADING_IDS.match(line.strip())
        if head is None:
            continue
        for token in re.findall(r"\d+", head.group()):
            fact = by_id.get(int(token))
            if fact is not None and fact not in chosen:
                chosen.append(fact)
    return chosen


def _filter(
    llm: LLMClient,
    cancel: threading.Event,
    task: str,
    query: str,
    candidates: list[Fact],
    render: Callable[[Fact], str],
    page_chars: int,
) -> list[Fact]:
    survivors: list[Fact] = []
    for page in _pages([render(fact) for fact in candidates], page_chars):
        survivors.extend(_selected(_ask(llm, cancel, task, query, page), candidates))
    return survivors


def search(
    llm: LLMClient,
    session: Session,
    query: str,
    context_size: int,
    chars_per_token: float,
    cancel: threading.Event,
) -> str:
    all_facts = facts(session)
    page_chars = max(MIN_PAGE_CHARS, int(prompt_budget(context_size) * chars_per_token / 2))
    candidates = all_facts
    for task, render in ((_SCAN_TASK, _scan_line), (_REFINE_TASK, _refine_line)):
        if not candidates:
            break
        candidates = _filter(llm, cancel, task, query, candidates, render, page_chars)
    if candidates:
        page = "\n".join(_refine_line(fact) for fact in candidates)
        reply = _ask(llm, cancel, _REDUCE_TASK, query, page)
        if _selected(reply, candidates):
            return f"{reply.strip()}\n{_RECOVERY_HINT}"
    index = fact_index(all_facts)
    tail = f"\n{index}" if index else ""
    return f"no relevant facts found for {query!r}{tail}"
