from pathlib import Path

import pytest

from pico.core.actions import (
    MAX_DELEGATE_DEPTH,
    RUNNER_ACTIONS,
    InvalidActionError,
    register_actions,
    vocabulary,
)
from pico.core.errors import ToolError
from pico.core.tools import ToolRegistry
from pico.llm.types import (
    ToolCall,
)
from pico.session import (
    Session,
    connect,
)
from tests.core.loop_fixtures import (
    make_session,
)


def test_register_actions_read_file_missing_path_raises_tool_error(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    with pytest.raises(ToolError):
        registry.execute(
            ToolCall(id="1", name="read_file", arguments={"path": str(tmp_path / "nope.txt")})
        )


def test_register_actions_populates_all_plain_tool_names() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    names = {spec.name for spec in registry.specs()}

    assert names == {
        "read_file",
        "write_file",
        "load_table",
        "sql",
        "note",
        "read_fact",
        "set_plan",
        "complete_step",
    }


def test_register_actions_has_no_placeholder_executors() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    for spec in registry.specs():
        with pytest.raises((InvalidActionError, ToolError)):
            registry.execute(ToolCall(id="1", name=spec.name, arguments={"bogus": True}))


def test_register_actions_read_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello")
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    result = registry.execute(ToolCall(id="1", name="read_file", arguments={"path": str(path)}))

    assert result == "hello"


def test_register_actions_write_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    result = registry.execute(
        ToolCall(id="1", name="write_file", arguments={"path": str(path), "content": "hi"})
    )

    assert "wrote" in result
    assert path.read_text() == "hi"


def test_register_actions_invalid_arguments_raise_invalid_action_error() -> None:
    registry = ToolRegistry()
    register_actions(registry, Session(connect(":memory:"), "s1"))

    with pytest.raises(InvalidActionError, match="missing required field"):
        registry.execute(ToolCall(id="1", name="read_file", arguments={}))


def test_vocabulary_lists_plain_tools_then_runner_actions() -> None:
    registry = ToolRegistry()
    register_actions(registry, make_session())

    specs = vocabulary(registry, 0)

    assert [spec.name for spec in specs] == [
        "read_file",
        "write_file",
        "load_table",
        "sql",
        "note",
        "read_fact",
        "set_plan",
        "complete_step",
        "shell",
        "answer",
        "search_facts",
        "delegate",
    ]
    assert specs[-4:] == [action.spec for action in RUNNER_ACTIONS.values()]


def test_vocabulary_omits_delegate_at_max_depth() -> None:
    registry = ToolRegistry()
    register_actions(registry, make_session(), depth=MAX_DELEGATE_DEPTH)

    names = [spec.name for spec in vocabulary(registry, MAX_DELEGATE_DEPTH)]

    assert "delegate" not in names
    assert "shell" in names
    assert names == [spec.name for spec in vocabulary(registry, 0) if spec.name != "delegate"]


def test_a_restricted_vocabulary_omits_delegate_at_max_depth() -> None:
    registry = ToolRegistry()
    register_actions(registry, make_session(), depth=MAX_DELEGATE_DEPTH)

    names = [
        spec.name
        for spec in vocabulary(registry, MAX_DELEGATE_DEPTH, ("answer", "delegate", "note"))
    ]

    assert "delegate" not in names
    assert names == ["answer", "note"]


def test_a_restricted_vocabulary_keeps_delegate_below_max_depth() -> None:
    registry = ToolRegistry()
    register_actions(registry, make_session(), depth=0)

    names = [spec.name for spec in vocabulary(registry, 0, ("answer", "delegate"))]

    assert names == ["answer", "delegate"]
