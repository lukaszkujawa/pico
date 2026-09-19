import json
from collections.abc import Mapping
from dataclasses import dataclass

from pico.session import (
    PlanSet,
    PlanStepCompleted,
    Session,
    ToolCallRecorded,
)

BOOKKEEPING_TOOLS = frozenset({"read_fact", "search_facts", "set_plan", "complete_step"})

_SIGNATURE_CHARS = 60
_ELLIPSIS = "…"
_INDEX_FACTS = 20
_INDEX_LINE_CHARS = 90


@dataclass(frozen=True)
class Fact:
    id: int
    content: str
    source: str
    arguments: Mapping[str, object]


def facts(session: Session) -> list[Fact]:
    return [
        Fact(id=seq, content=event.result, source=event.name, arguments=event.arguments)
        for seq, event in session.tree_records()
        if isinstance(event, ToolCallRecorded)
        and not event.is_error
        and event.name not in BOOKKEEPING_TOOLS
    ]


def _elide_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = (limit - len(_ELLIPSIS)) // 2
    tail = limit - len(_ELLIPSIS) - head
    return f"{text[:head]}{_ELLIPSIS}{text[len(text) - tail :]}"


def render_call(name: str, arguments: Mapping[str, object]) -> str:
    args = ", ".join(str(value) for value in arguments.values())
    call = f"{name}({args})" if args else f"{name}()"
    return _elide_middle(call, _SIGNATURE_CHARS)


def _fact_preview(content: str, limit: int) -> str:
    return " ".join(content.split())[:limit]


def _index_line(fact: Fact) -> str:
    signature = render_call(fact.source, fact.arguments)
    prefix = f"[{fact.id}] {signature}: "
    preview = _fact_preview(fact.content, max(0, _INDEX_LINE_CHARS - len(prefix)))
    return f"{prefix}{preview}"


def _newest_per_call(all_facts: list[Fact]) -> list[Fact]:
    newest: dict[str, Fact] = {}
    for fact in all_facts:
        newest[f"{fact.source}{json.dumps(fact.arguments, sort_keys=True)}"] = fact
    return list(newest.values())


def fact_index(all_facts: list[Fact]) -> str:
    if not all_facts:
        return ""
    distinct = _newest_per_call(all_facts)
    shown = distinct[-_INDEX_FACTS:]
    overflow = len(distinct) - len(shown)
    lines = [_index_line(fact) for fact in shown]
    if overflow:
        lines.append(f"+{overflow} earlier facts")
    lines.append("Call read_fact(id) to recover any fact in full.")
    return "\n".join(lines)


@dataclass(frozen=True)
class PlanStep:
    text: str
    done: bool


@dataclass(frozen=True)
class Plan:
    steps: tuple[PlanStep, ...]


def plan(session: Session) -> Plan | None:
    steps: list[PlanStep] | None = None
    for event in session.events():
        match event:
            case PlanSet(steps=texts):
                steps = [PlanStep(text=text, done=False) for text in texts]
            case PlanStepCompleted(index=index):
                if steps is not None and 0 <= index < len(steps):
                    steps[index] = PlanStep(text=steps[index].text, done=True)
            case _:
                pass
    return None if steps is None else Plan(steps=tuple(steps))


def render_plan(current: Plan) -> str:
    return "\n".join(
        f"[{'x' if step.done else ' '}] {index}. {step.text}"
        for index, step in enumerate(current.steps)
    )
