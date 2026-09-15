import pytest

from pico.core.errors import UnknownToolError
from pico.core.tools import Tool, ToolRegistry
from pico.llm.types import ToolCall, ToolSpec


def _echo_tool() -> Tool:
    return Tool(
        spec=ToolSpec(name="echo", description="echoes input", parameters={"type": "object"}),
        execute=lambda args: str(args.get("text", "")),
    )


def test_register_and_execute() -> None:
    registry = ToolRegistry()
    registry.register(_echo_tool())

    result = registry.execute(ToolCall(id="1", name="echo", arguments={"text": "hi"}))

    assert result == "hi"


def test_specs_lists_registered_tools() -> None:
    registry = ToolRegistry()
    registry.register(_echo_tool())

    specs = registry.specs()

    assert specs == [
        ToolSpec(name="echo", description="echoes input", parameters={"type": "object"})
    ]


def test_execute_unknown_tool_raises() -> None:
    registry = ToolRegistry()

    with pytest.raises(UnknownToolError):
        registry.execute(ToolCall(id="1", name="missing", arguments={}))
