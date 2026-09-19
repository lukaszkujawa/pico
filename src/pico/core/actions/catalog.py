from pico.core.actions.answer import ANSWER_ACTION
from pico.core.actions.context import RunnerAction
from pico.core.actions.delegate import DELEGATE_ACTION, MAX_DELEGATE_DEPTH
from pico.core.actions.facts import SEARCH_ACTION, fact_recall_tool, note_tool
from pico.core.actions.files import edit_file_tool, read_file_tool, write_file_tool
from pico.core.actions.planning import complete_step_tool, set_plan_tool
from pico.core.actions.scratch import load_table_tool, sql_tool
from pico.core.actions.shell import SHELL_ACTION
from pico.core.actions.view import view_image_tool
from pico.core.scratch import Scratch
from pico.core.tools import ToolRegistry
from pico.llm.types import ToolSpec
from pico.session import Session

RUNNER_ACTIONS: dict[str, RunnerAction] = {
    action.spec.name: action
    for action in (SHELL_ACTION, ANSWER_ACTION, SEARCH_ACTION, DELEGATE_ACTION)
}


def register_actions(
    registry: ToolRegistry, session: Session, depth: int = 0, vision: bool = False
) -> None:
    registry.register(read_file_tool())
    registry.register(write_file_tool())
    registry.register(edit_file_tool())
    scratch = Scratch(session, in_memory=depth > 0)
    registry.register(load_table_tool(scratch))
    registry.register(sql_tool(scratch))
    registry.register(note_tool())
    registry.register(fact_recall_tool(session))
    registry.register(set_plan_tool(session))
    registry.register(complete_step_tool(session))
    if vision:
        registry.register(view_image_tool())


def vocabulary(
    tools: ToolRegistry, depth: int, allowed: tuple[str, ...] | None = None
) -> list[ToolSpec]:
    specs = [*tools.specs(), *(action.spec for action in RUNNER_ACTIONS.values())]
    if depth >= MAX_DELEGATE_DEPTH:
        specs = [spec for spec in specs if spec.name != "delegate"]
    if allowed is None:
        return specs
    by_name = {spec.name: spec for spec in specs}
    return [by_name[name] for name in allowed if name in by_name]
