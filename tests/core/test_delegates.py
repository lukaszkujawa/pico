import threading
from collections.abc import Iterator
from pathlib import Path

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions
from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    GenerationCompleted,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.ledger import facts
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.dispatch import MAX_INVALID_ACTION_ATTEMPTS
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import Answered
from pico.core.loop.subruns import (
    CHILD_BUDGETS,
)
from pico.core.stuckness import STUCK_THRESHOLD
from pico.core.tools import ToolRegistry
from pico.llm.types import (
    GenerationComplete,
    Message,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)
from pico.session import (
    AssistantMessageRecorded,
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
    connect,
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    answer_turn,
    delegate_turn,
    echo_registry,
    make_session,
    text_turn,
)
from tests.llm_fakes import NoModels


def test_delegate_call_that_answers_records_fact_on_parent() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert tool_events[-1] == ToolCallRecorded(
        name="delegate",
        arguments={"question": "what is x?"},
        result="x is 1",
        is_error=False,
    )
    parent_facts = facts(session)
    assert parent_facts[-1].content == "x is 1"
    assert parent_facts[-1].source == "delegate"


def test_delegate_call_exhausting_budget_without_answer_is_error() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    turns: list[list[StreamEvent]] = [
        [ToolCallReady(tool_call=delegate_call), GenerationComplete(finish_reason="tool_calls")],
    ]
    turns.extend(
        [
            ToolCallReady(
                tool_call=ToolCall(id=str(i), name="note", arguments={"content": f"looking {i}"})
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(CHILD_BUDGETS[0])
    )
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    tool_events = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert tool_events[-1].name == "delegate"
    assert tool_events[-1].is_error is True


def test_delegate_that_dies_of_budget_returns_a_partial_answer() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    turns: list[list[StreamEvent]] = [
        [ToolCallReady(tool_call=delegate_call), GenerationComplete(finish_reason="tool_calls")],
    ]
    turns.extend(
        [
            ToolCallReady(
                tool_call=ToolCall(id=str(i), name="note", arguments={"content": f"looking {i}"})
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(CHILD_BUDGETS[0])
    )
    turns.append(answer_turn("x is probably 1"))
    turns.append(answer_turn("done"))
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    delegated = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "delegate"
    )
    assert delegated.is_error is False
    assert delegated.result == (
        f"partial — the generation budget of {CHILD_BUDGETS[0]} is spent:\nx is probably 1"
    )
    assert runner.state == Answered("done")


def test_delegate_can_run_shell_and_answer_with_verify(tmp_path: Path) -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    marker = tmp_path / "found.txt"
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1",
                        name="shell",
                        arguments={"command": f"echo 1 > {marker}"},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2",
                        name="answer",
                        arguments={
                            "content": "x is 1",
                            "citations": [],
                            "verify": f"test -f {marker}",
                        },
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_tool_events = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded)
    ]
    assert [event.name for event in child_tool_events] == ["shell", "answer"]
    assert all(not event.is_error for event in child_tool_events)
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert parent.name == "delegate"
    assert parent.result.startswith("x is 1")


def test_delegate_can_delegate_further_and_session_ids_nest() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "outer"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c1", name="delegate", arguments={"question": "inner"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="g1", name="answer", arguments={"content": "deep", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2", name="answer", arguments={"content": "outer done", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    grandchild = session.child("delegate/2").child("delegate/2")
    assert grandchild.session_id == "s1/delegate/2/delegate/2"
    assert [event for event in grandchild.events() if isinstance(event, UserMessageRecorded)] == [
        UserMessageRecorded(content="inner")
    ]
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert parent.result == "outer done"


def test_delegate_at_max_depth_is_not_offered_the_delegate_tool() -> None:
    conn = connect(":memory:")
    session = Session(conn, "s1")
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "deeper"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "no deeper", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    tools = ToolRegistry()
    register_actions(tools, session, depth=MAX_DELEGATE_DEPTH - 1)

    runner = LoopRunner(
        client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH - 1
    )
    runner.execute()

    child_specs = client.seen_tools[1]
    assert "delegate" not in {spec.name for spec in child_specs}
    assert "delegate" in {spec.name for spec in client.seen_tools[0]}


def test_cancelling_parent_mid_delegate_cancels_the_child_run() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    cancel = threading.Event()
    child_turn: list[StreamEvent] = [
        TextDelta(text="pondering"),
        GenerationComplete(finish_reason="stop"),
    ]

    class CancelDuringChild(NoModels):
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            self.calls += 1
            if self.calls == 1:
                yield ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                )
                yield GenerationComplete(finish_reason="tool_calls")
                return
            cancel.set()
            yield from child_turn

    runner = LoopRunner(
        CancelDuringChild(), echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
    runner.execute()

    child_events = list(session.child("delegate/2").events())
    assert [event for event in child_events if isinstance(event, AssistantMessageRecorded)] == []
    parent = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert parent == []


def test_repeating_delegate_is_stopped_by_stuckness() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    repeat: list[StreamEvent] = [
        ToolCallReady(tool_call=ToolCall(id="c", name="shell", arguments={"command": "false"})),
        GenerationComplete(finish_reason="tool_calls"),
    ]
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        *[list(repeat) for _ in range(CHILD_BUDGETS[0])],
        [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
    ]
    client = ScriptedClient(turns)
    tools = ToolRegistry()
    register_actions(tools, session)

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_calls = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded)
    ]
    assert len(child_calls) == STUCK_THRESHOLD + 1
    parent = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert parent.is_error is True
    assert "delegate failed" in parent.result
    assert "stuck" in parent.result


def test_delegate_child_session_is_distinct_from_parent() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child_session = session.child("delegate/2")
    child_user_messages = [
        event for event in child_session.events() if isinstance(event, UserMessageRecorded)
    ]
    assert child_user_messages == [UserMessageRecorded(content="what is x?")]
    parent_user_messages = [
        event for event in session.events() if isinstance(event, UserMessageRecorded)
    ]
    assert parent_user_messages == [UserMessageRecorded(content="hi")]


def test_delegate_child_stream_events_do_not_appear_on_parent_bus() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=delegate_call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                TextDelta(text="pondering"),
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(8)]
    assert events == [
        RunStarted(),
        GenerationCompleted(iteration=1),
        ToolCallStarted(id="0", name="delegate", arguments={"question": "what is x?"}),
        ToolCallFinished(
            id="0", tool_call=delegate_call, result="x is 1", is_error=False, fact_id=None
        ),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="done"),
        GenerationCompleted(iteration=2),
        AssistantTextFinished(id="1"),
    ]


def test_delegate_read_fact_cannot_reach_parent_facts() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="shell", arguments={}, result="parent secret", is_error=False)
    )
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="1", name="delegate", arguments={"question": "what is x?"}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(tool_call=ToolCall(id="c1", name="read_fact", arguments={"id": 2})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2", name="answer", arguments={"content": "x is 1", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    child = session.child("delegate/3")
    recalled = [
        event
        for event in child.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    ]
    assert recalled[0].is_error is True
    assert "parent secret" not in recalled[0].result


def _delegate_run(
    fields: object, answers: list[str], question: str = "how many?"
) -> tuple[Session, ScriptedClient]:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    arguments: dict[str, object] = {"question": question}
    if fields is not None:
        arguments["fields"] = fields
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(tool_call=ToolCall(id="1", name="delegate", arguments=arguments)),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        *[
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id=f"c{index}",
                        name="answer",
                        arguments={"content": content, "citations": []},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ]
            for index, content in enumerate(answers)
        ],
        answer_turn(),
    ]
    client = ScriptedClient(turns)
    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()
    return session, client


def _parent_delegate_result(session: Session) -> ToolCallRecorded:
    return next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "delegate"
    )


def _child_answers(session: Session) -> list[ToolCallRecorded]:
    return [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded)
    ]


def test_typed_delegate_returns_conforming_json_verbatim() -> None:
    record = '{"count": 3, "unit": "files"}'
    session, client = _delegate_run({"count": "number", "unit": "string"}, [record])

    result = _parent_delegate_result(session)
    assert result.is_error is False
    assert result.result == record
    child_question = next(
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, UserMessageRecorded)
    )
    assert '"count": <number>' in child_question.content
    assert child_question.content.startswith("how many?")
    assert len(client.seen_messages) == 3


def test_typed_delegate_rejects_non_json_answer_and_child_retries() -> None:
    session, _ = _delegate_run({"count": "number"}, ["three", '{"count": 3}'])

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "did not parse as JSON" in answers[0].result
    assert answers[1].is_error is False
    assert _parent_delegate_result(session).result == '{"count": 3}'


def test_typed_delegate_rejects_missing_field_and_child_retries() -> None:
    session, _ = _delegate_run(
        {"count": "number", "unit": "string"},
        ['{"count": 3}', '{"count": 3, "unit": "files"}'],
    )

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "missing required field(s): ['unit']" in answers[0].result
    assert answers[1].is_error is False


def test_typed_delegate_rejects_extra_field_and_child_retries() -> None:
    session, _ = _delegate_run({"count": "number"}, ['{"count": 3, "extra": 1}', '{"count": 3}'])

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "unexpected field(s): ['extra']" in answers[0].result
    assert answers[1].is_error is False


def test_typed_delegate_rejects_wrong_typed_field_and_child_retries() -> None:
    session, _ = _delegate_run({"count": "number"}, ['{"count": "3"}', '{"count": 3}'])

    answers = _child_answers(session)
    assert answers[0].is_error is True
    assert "field 'count' must be a number, got str" in answers[0].result
    assert answers[1].is_error is False


def test_untyped_delegate_passes_prose_answer_unchanged() -> None:
    session, _ = _delegate_run(None, ["there are three files"], question="how many?")

    result = _parent_delegate_result(session)
    assert result.is_error is False
    assert result.result == "there are three files"
    child_question = next(
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, UserMessageRecorded)
    )
    assert child_question.content == "how many?"


def test_invalid_delegate_fields_is_an_invalid_action_on_the_parent() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id=f"{index}",
                    name="delegate",
                    arguments={"question": "q", "fields": {"when": "date"}},
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for index in range(MAX_INVALID_ACTION_ATTEMPTS)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert all(event.is_error for event in recorded)
    assert "unknown type 'date'" in recorded[0].result
    assert runner.dispatch.invalid_action_attempts == MAX_INVALID_ACTION_ATTEMPTS
    assert list(session.child("delegate/2").events()) == []


def test_non_object_delegate_fields_is_an_invalid_action_on_the_parent() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="1",
                        name="delegate",
                        arguments={"question": "q", "fields": ["count"]},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert "must be an object" in recorded.result


def test_delegates_across_turns_get_distinct_child_sessions() -> None:
    session = make_session()

    session.append(UserMessageRecorded(content="first"))
    client = ScriptedClient(delegate_turn("first question"))
    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    session.append(UserMessageRecorded(content="second"))
    client = ScriptedClient(delegate_turn("second question"))
    LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    first_child_questions = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, UserMessageRecorded)
    ]
    second_child_questions = [
        event
        for event in session.child("delegate/5").events()
        if isinstance(event, UserMessageRecorded)
    ]
    assert first_child_questions == [UserMessageRecorded(content="first question")]
    assert second_child_questions == [UserMessageRecorded(content="second question")]


def test_delegate_call_at_max_depth_is_rejected_without_spawning_a_child() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )
    tools = ToolRegistry()
    register_actions(tools, session, depth=MAX_DELEGATE_DEPTH)

    runner = LoopRunner(
        client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG, depth=MAX_DELEGATE_DEPTH
    )
    runner.execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert runner.dispatch.invalid_action_attempts == 1
    assert list(session.child("delegate/2").events()) == []


def test_delegate_at_max_depth_reports_it_is_unavailable() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            text_turn("done"),
        ]
    )

    runner = LoopRunner(
        client,
        echo_registry(),
        Bus(),
        session,
        128_000,
        DEFAULT_LOOP_CONFIG,
        depth=MAX_DELEGATE_DEPTH,
    )
    runner.execute()

    recorded = next(event for event in session.events() if isinstance(event, ToolCallRecorded))
    assert recorded.is_error is True
    assert "not available at this depth" in recorded.result
    assert runner.dispatch.invalid_action_attempts == 1


def test_delegate_that_only_narrates_returns_its_last_narration_marked_unverified() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    delegate_call = ToolCall(id="1", name="delegate", arguments={"question": "what is x?"})
    turns: list[list[StreamEvent]] = [
        [ToolCallReady(tool_call=delegate_call), GenerationComplete(finish_reason="tool_calls")],
    ]
    turns.extend(
        [TextDelta(text=f"thinking {index}"), GenerationComplete(finish_reason="stop")]
        for index in range(10)
    )
    turns.append(answer_turn())
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    delegated = _parent_delegate_result(session)
    assert delegated.is_error is False
    assert delegated.result == "thinking 5"
    assert runner.state == Answered("done")


def test_parent_can_read_and_cite_a_fact_minted_inside_a_delegate() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="note", arguments={"content": "the port is 8421"}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c2", name="answer", arguments={"content": "done", "citations": [1]}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(tool_call=ToolCall(id="2", name="read_fact", arguments={"id": 1})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="3", name="answer", arguments={"content": "8421", "citations": [1]}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recalled = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    )
    assert recalled.result == "the port is 8421"
    assert recalled.is_error is False
    assert runner.state == Answered("8421")


def test_child_sees_parent_facts_in_its_briefing_index() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="note", arguments={}, result="the port is 8421", is_error=False)
    )
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c1", name="answer", arguments={"content": "ok", "citations": [1]}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    briefing = client.seen_messages[1][1]
    assert "[1] note(): the port is 8421" in briefing.content
