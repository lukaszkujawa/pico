from collections.abc import Iterator

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions
from pico.core.bus import Bus
from pico.core.context import (
    PLAN_INLINE_HINT,
    PLAN_ORCHESTRATED_HINT,
    RECENT_UNITS,
    message_text,
)
from pico.core.ledger import facts
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.decision import DECISION_GRACE, MAX_CROSSROADS
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import Answered, Failed
from pico.core.loop.subruns import (
    MAX_STEP_STEPS,
    root_task,
)
from pico.core.stuckness import STUCK_THRESHOLD
from pico.core.tools import ToolRegistry
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    StreamEvent,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)
from pico.session import (
    PlanStepCompleted,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    answer_turn,
    make_session,
    set_plan_turn,
    text_turn,
)
from tests.llm_fakes import NoModels


def _step_children(session: Session) -> list[Session]:
    rows = session.connection.execute(
        "SELECT DISTINCT session_id FROM events "
        "WHERE session_id LIKE ? AND session_id NOT LIKE ? ORDER BY id",
        (f"{session.session_id}/step/%", f"{session.session_id}/step/%/%"),
    ).fetchall()
    return [Session(session.connection, str(row[0])) for row in rows]


def _step_session() -> tuple[Session, ToolRegistry]:
    session = make_session()
    session.append(UserMessageRecorded(content="survey the repository"))
    registry = ToolRegistry()
    register_actions(registry, session)
    return session, registry


def test_each_plan_step_runs_in_its_own_child_session() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files", "name the largest"]),
            answer_turn("there are 12 files"),
            text_turn("good"),
            answer_turn("loop.py is largest"),
            answer_turn("12 files, loop.py is largest"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("12 files, loop.py is largest")
    first, second = _step_children(session)
    assert first.session_id != second.session_id
    assert list(first.events()) != list(second.events())
    assert "count the files" in root_task(first)
    assert "name the largest" in root_task(second)


def test_second_step_prompt_carries_the_first_steps_result_and_plan_progress() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files", "name the largest"]),
            answer_turn("there are 12 files"),
            text_turn("good"),
            answer_turn("loop.py is largest"),
            answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    second = root_task(_step_children(session)[1])
    assert "survey the repository" in second
    assert "[x] 0. count the files" in second
    assert "[ ] 1. name the largest" in second
    assert "there are 12 files" in second


def test_accepted_child_answer_settles_the_step_without_complete_step() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [set_plan_turn(["count the files"]), answer_turn("there are 12 files"), answer_turn()]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    assert PlanStepCompleted(index=0) in list(session.events())
    assert not any(
        isinstance(event, ToolCallRecorded) and event.name == "complete_step"
        for event in session.events()
    )
    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.result == "there are 12 files"
    assert recorded.is_error is False


def test_step_result_is_a_fact_recoverable_from_anywhere_in_the_tree() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [set_plan_turn(["count the files"]), answer_turn("there are 12 files"), answer_turn()]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    step_facts = [fact for fact in facts(session) if fact.source == "step"]
    assert [fact.content for fact in step_facts] == ["there are 12 files"]
    assert facts(_step_children(session)[0]) == facts(session)


def test_settled_step_is_not_re_run_when_the_parent_does_nothing() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            answer_turn("there are 12 files"),
            text_turn("thinking"),
            answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    steps = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    ]
    assert len(steps) == 1


def test_revising_the_plan_changes_which_step_runs_next() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            answer_turn("there are 12 files"),
            set_plan_turn(["count the files", "name the largest"]),
            answer_turn("loop.py is largest"),
            answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    steps = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    ]
    assert [event.arguments["step"] for event in steps] == ["count the files", "count the files"]
    assert [event.result for event in steps] == ["there are 12 files", "loop.py is largest"]


def test_failed_step_is_retried_once_then_fails_the_node() -> None:
    session, registry = _step_session()

    class PlanThenFailingChildren(NoModels):
        def __init__(self) -> None:
            self.turns = [set_plan_turn(["count the files"])]

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            if any("Your step is step" in message.content for message in messages):
                raise LLMError("connection lost")
            yield from (self.turns.pop(0) if self.turns else text_turn("waiting"))

    runner = LoopRunner(
        PlanThenFailingChildren(), registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG
    )
    runner.execute()

    steps = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    ]
    assert len(steps) == 2
    assert all(event.is_error for event in steps)
    assert isinstance(runner.state, Failed)
    assert "count the files" in runner.state.reason
    assert not any(isinstance(event, PlanStepCompleted) for event in session.events())


def test_parent_context_after_a_step_holds_the_result_but_no_child_transcript() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c1", name="note", arguments={"content": "private"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            answer_turn("there are 12 files"),
            answer_turn("done"),
        ]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    parent_window = "".join(message_text(message) for message in client.seen_messages[-1][2:])
    assert "there are 12 files" in parent_window
    assert "private" not in parent_window


def test_a_step_child_may_plan_and_recurse_into_its_own_children() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["survey the code"]),
            set_plan_turn(["read loop.py"]),
            answer_turn("loop.py drives the run"),
            answer_turn("the survey is done"),
            answer_turn("all done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    [child] = _step_children(session)
    [grandchild] = _step_children(child)
    assert "read loop.py" in root_task(grandchild)
    assert runner.state == Answered("all done")


def test_plan_at_max_depth_runs_inline_with_no_child_spawned() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            [
                ToolCallReady(
                    tool_call=ToolCall(id="2", name="complete_step", arguments={"index": 0})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            answer_turn("done"),
        ]
    )

    runner = LoopRunner(
        client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    assert runner.state == Answered("done")
    assert PlanStepCompleted(index=0) in list(session.events())
    assert not any(
        isinstance(event, ToolCallRecorded) and event.name == "step" for event in session.events()
    )


def test_a_run_without_a_plan_spawns_no_step_children() -> None:
    session, registry = _step_session()
    client = ScriptedClient([answer_turn("nothing to plan")])

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.state == Answered("nothing to plan")
    assert not any(
        isinstance(event, ToolCallRecorded) and event.name == "step" for event in session.events()
    )


def test_orchestrated_briefing_replaces_the_complete_step_hint() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [set_plan_turn(["count the files"]), answer_turn("12"), answer_turn("done")]
    )

    LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    briefing = client.seen_messages[-1][1]
    assert PLAN_ORCHESTRATED_HINT in briefing.content
    assert PLAN_INLINE_HINT not in briefing.content


def test_a_step_child_that_dies_returns_a_partial_answer_marked_partial() -> None:
    session, registry = _step_session()
    repeat: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="c", name="shell", arguments={"command": "true"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            *[list(repeat) for _ in range(STUCK_THRESHOLD)],
            answer_turn("I counted 7 before running out"),
            answer_turn("done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is False
    assert recorded.result.startswith("partial — you are stuck")
    assert "I counted 7 before running out" in recorded.result
    assert PlanStepCompleted(index=0) in list(session.events())
    assert runner.state == Answered("done")


def test_a_step_child_that_ignores_two_crossroads_answers_partially() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            *[
                [
                    ToolCallReady(
                        tool_call=ToolCall(
                            id="c", name="shell", arguments={"command": f"echo {index}"}
                        )
                    ),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
                for index in range(MAX_STEP_STEPS)
            ],
            answer_turn("done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is True
    assert f"{MAX_CROSSROADS} decision points passed" in recorded.result


def test_a_step_child_answering_at_its_crossroads_settles_the_step() -> None:
    session, registry = _step_session()
    client = ScriptedClient(
        [
            set_plan_turn(["count the files"]),
            *[
                [
                    ToolCallReady(
                        tool_call=ToolCall(
                            id="c", name="shell", arguments={"command": f"echo {index}"}
                        )
                    ),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
                for index in range(RECENT_UNITS + DECISION_GRACE + 2)
            ],
            answer_turn("I counted 7 files"),
            answer_turn("done"),
        ]
    )

    runner = LoopRunner(client, registry, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "step"
    )
    assert recorded.is_error is False
    assert recorded.result == "I counted 7 files"
    assert runner.state == Answered("done")
