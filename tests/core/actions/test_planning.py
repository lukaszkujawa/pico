import pytest

from pico.core.actions import (
    CompleteStep,
    InvalidActionError,
    SetPlan,
    complete_step_tool,
    register_actions,
    set_plan_tool,
    vocabulary,
)
from pico.core.errors import ToolError
from pico.core.tools import ToolRegistry
from pico.session import (
    PlanSet,
    PlanStepCompleted,
    Session,
    connect,
)
from tests.core.loop_fixtures import (
    decision_session,
    make_session,
)


def _planless_session() -> Session:
    return Session(connect(":memory:"), "s1")


def test_set_plan_from_arguments() -> None:
    assert SetPlan.from_arguments({"steps": ["one", "two"]}) == SetPlan(steps=("one", "two"))


def test_set_plan_from_arguments_empty_list_is_invalid() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        SetPlan.from_arguments({"steps": []})


def test_set_plan_from_arguments_non_string_element_is_invalid() -> None:
    with pytest.raises(InvalidActionError, match="list of strings"):
        SetPlan.from_arguments({"steps": ["one", 2]})


def test_complete_step_from_arguments() -> None:
    assert CompleteStep.from_arguments({"index": 3}) == CompleteStep(index=3)


def test_set_plan_appends_event_and_returns_checklist() -> None:
    session = _planless_session()

    result = set_plan_tool(session).execute({"steps": ["read the file", "write the answer"]})

    assert list(session.events()) == [PlanSet(steps=("read the file", "write the answer"))]
    assert result == "plan set:\n[ ] 0. read the file\n[ ] 1. write the answer"


def test_set_plan_with_empty_steps_raises_and_appends_nothing() -> None:
    session = _planless_session()

    with pytest.raises(InvalidActionError, match="must not be empty"):
        set_plan_tool(session).execute({"steps": []})

    assert list(session.events()) == []


def test_set_plan_with_malformed_steps_raises_and_appends_nothing() -> None:
    session = _planless_session()

    with pytest.raises(InvalidActionError, match="must be a list"):
        set_plan_tool(session).execute({"steps": "one"})

    assert list(session.events()) == []


def test_complete_step_checks_the_box() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one", "two"]})

    result = complete_step_tool(session).execute({"index": 0})

    assert list(session.events())[-1] == PlanStepCompleted(index=0)
    assert result == "step 0 done:\n[x] 0. one\n[ ] 1. two"


def test_complete_step_without_a_plan_raises_tool_error() -> None:
    session = _planless_session()

    with pytest.raises(ToolError, match="no plan set"):
        complete_step_tool(session).execute({"index": 0})

    assert list(session.events()) == []


def test_complete_step_out_of_range_raises_tool_error_and_appends_nothing() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one"]})

    with pytest.raises(ToolError, match="no plan step at index 5"):
        complete_step_tool(session).execute({"index": 5})

    assert list(session.events()) == [PlanSet(steps=("one",))]


def test_complete_step_already_done_raises_tool_error_and_appends_nothing() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one"]})
    complete_step_tool(session).execute({"index": 0})
    before = list(session.events())

    with pytest.raises(ToolError, match="already done"):
        complete_step_tool(session).execute({"index": 0})

    assert list(session.events()) == before


def test_complete_step_non_integer_index_raises_invalid_action_error() -> None:
    session = _planless_session()
    set_plan_tool(session).execute({"steps": ["one"]})

    with pytest.raises(InvalidActionError, match="must be a int"):
        complete_step_tool(session).execute({"index": "0"})


def test_set_plan_description_names_fresh_agents_and_self_contained_steps() -> None:
    registry = ToolRegistry()
    register_actions(registry, make_session())

    description = next(spec for spec in registry.specs() if spec.name == "set_plan").description

    assert "fresh agent" in description
    assert "stand alone" in description


def test_set_plan_description_leads_with_the_reason_to_plan() -> None:
    description = next(
        spec.description for spec in vocabulary(decision_session()[1], 0) if spec.name == "set_plan"
    )

    assert description.startswith("Break work too big for one context into steps")
    assert description.index("fresh context") < description.index("Replace the current plan")
