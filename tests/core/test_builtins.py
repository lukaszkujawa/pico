import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions
from pico.core.bus import Bus
from pico.core.errors import ToolError
from pico.core.events import (
    AnswerSettled,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallResultDelta,
    ToolCallStarted,
)
from pico.core.ledger import facts
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.builtins import RUNNER_ACTIONS, vocabulary
from pico.core.loop.runner import LoopRunner
from pico.core.stuckness import STUCK_THRESHOLD
from pico.core.tools import ToolRegistry
from pico.llm.errors import LLMError
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
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    answer_turn,
    decision_session,
    echo_registry,
    make_session,
)


def test_valid_answer_call_ends_run_and_records_result() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": []})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(
            id="0", name="answer", arguments={"content": "the answer", "citations": []}
        ),
        AnswerSettled(id="0", content="the answer", accepted=True, reason=None, verify=None),
        RunFinished(),
    ]
    assert runner.final_answer == "the answer"
    assert list(session.events())[-1] == ToolCallRecorded(
        name="answer",
        arguments={"content": "the answer", "citations": []},
        result="the answer",
        is_error=False,
    )


def test_answer_citing_a_bookkeeping_call_seq_is_rejected() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="note", arguments={}, result="finding", is_error=False))
    session.append(
        ToolCallRecorded(name="read_fact", arguments={"id": 2}, result="finding", is_error=False)
    )
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [3]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result


def test_invalid_answer_call_continues_run_instead_of_ending() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    bad_call = ToolCall(id="1", name="answer", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=bad_call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events[:3] == [
        RunStarted(),
        GenerationCompleted(),
        ToolCallStarted(id="0", name="answer", arguments={}),
    ]
    settled = events[3]
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.reason == "missing required field 'content'"
    assert runner.final_answer is None


def test_answer_citing_known_fact_ends_run() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(name="read_file", arguments={}, result="content", is_error=False)
    )
    fact_id = facts(session)[0].id
    call = ToolCall(
        id="1", name="answer", arguments={"content": "the answer", "citations": [fact_id]}
    )
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "the answer"


def test_answer_citing_unknown_fact_continues_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [0]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    settled = next(event for event in events if isinstance(event, AnswerSettled))
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.reason is not None
    assert "unknown fact citation" in settled.reason


def test_answer_citing_seq_of_error_call_continues_run() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))
    call = ToolCall(id="1", name="answer", arguments={"content": "the answer", "citations": [2]})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    last_tool_event = [event for event in session.events() if isinstance(event, ToolCallRecorded)][
        -1
    ]
    assert last_tool_event.is_error is True
    assert "unknown fact citation" in last_tool_event.result


def test_shell_tool_call_publishes_result_deltas_before_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    call = ToolCall(id="1", name="shell", arguments={"command": "echo one; echo two"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break

    finished_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolCallFinished)
    )
    delta_indices = [
        index for index, event in enumerate(events) if isinstance(event, ToolCallResultDelta)
    ]
    assert delta_indices
    assert all(index < finished_index for index in delta_indices)
    finished = events[finished_index]
    assert isinstance(finished, ToolCallFinished)
    assert finished.result == "one\ntwo\n"


def test_model_recovers_a_truncated_fact_via_read_fact_and_cites_it() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="what is in the log?"))
    content = "needle " * 2000
    session.append(ToolCallRecorded(name="shell", arguments={}, result=content, is_error=False))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="read_fact", arguments={"id": 1})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="2",
                        name="answer",
                        arguments={"content": "it is needles", "citations": [1]},
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 2000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    handle = client.seen_messages[0][-1]
    assert handle.tool_result is not None
    assert "fact 1 shell() truncated" in handle.tool_result.content
    assert "call read_fact(1) for the full content" in handle.tool_result.content

    recalled = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    ]
    assert recalled[0].result == content
    assert recalled[0].is_error is False
    assert runner.final_answer == "it is needles"


def test_read_fact_with_unknown_id_is_recorded_as_error() -> None:
    bus = Bus()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = ScriptedClient(
        [
            [
                ToolCallReady(tool_call=ToolCall(id="1", name="read_fact", arguments={"id": 404})),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert recorded[0].is_error is True
    assert "404" in recorded[0].result
    assert facts(session) == []
    assert runner.dispatch.invalid_action_attempts == 0


def test_answer_with_passing_verification_ends_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1", name="answer", arguments={"content": "done", "citations": [], "verify": "true"}
    )
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "done"
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is False
    assert recorded.result == "done\n\nverified: true"

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    settled = next(event for event in events if isinstance(event, AnswerSettled))
    assert isinstance(settled, AnswerSettled)
    assert (settled.content, settled.accepted, settled.reason, settled.verify) == (
        "done",
        True,
        None,
        "true",
    )
    assert "verified:" not in settled.content


def test_answer_with_failing_verification_is_rejected_and_run_continues() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1",
        name="answer",
        arguments={
            "content": "done",
            "citations": [],
            "verify": "echo missing output >&2; exit 3",
        },
    )
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="fixing"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is True
    assert "verification failed (exit 3)" in recorded.result
    assert "missing output" in recorded.result
    assert list(session.events())[-1] == AssistantMessageRecorded(content="fixing", thinking="")

    events: list[object] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            break
    settled = next(event for event in events if isinstance(event, AnswerSettled))
    assert isinstance(settled, AnswerSettled)
    assert settled.accepted is False
    assert settled.verify == "echo missing output >&2; exit 3"
    assert settled.reason is not None
    assert "verification failed (exit 3)" in settled.reason
    assert "missing output" in settled.reason


def test_answer_verification_sees_the_working_directory(tmp_path: Path) -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    target = tmp_path / "greeting.txt"
    call = ToolCall(
        id="1",
        name="answer",
        arguments={
            "content": "wrote it",
            "citations": [],
            "verify": f"test -f {target}",
        },
    )
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="fixing"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None

    target.write_text("hello")
    session = make_session("s2")
    session.append(UserMessageRecorded(content="hi"))
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )
    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "wrote it"


def test_answer_without_verification_result_is_the_content_alone() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(id="1", name="answer", arguments={"content": "done", "citations": []})
    client = ScriptedClient(
        [[ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")]]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer == "done"
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.result == "done"
    assert recorded.is_error is False


def test_repeated_failing_verification_hits_stuckness_not_invalid_action_cap() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    turns: list[list[StreamEvent]] = [
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id=str(index),
                    name="answer",
                    arguments={"content": f"done {index}", "citations": [], "verify": "false"},
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for index in range(STUCK_THRESHOLD + 3)
    ]
    client = ScriptedClient(turns)

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    assert runner.final_answer is None
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)]
    assert len(recorded) == STUCK_THRESHOLD + 1
    assert all(event.is_error for event in recorded)
    assert runner.error is not None
    assert "stuck" in runner.error


def test_answer_verification_timeout_is_a_tool_error_not_an_invalid_action() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    call = ToolCall(
        id="1", name="answer", arguments={"content": "done", "citations": [], "verify": "sleep 5"}
    )
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="fixing"), GenerationComplete(finish_reason="stop")],
        ]
    )

    runner = LoopRunner(client, echo_registry(), Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    with patch(
        "pico.core.loop.builtins.Shell.run", side_effect=ToolError("command timed out after 30s")
    ):
        runner.execute()

    assert runner.final_answer is None
    assert runner.dispatch.invalid_action_attempts == 0
    recorded = [event for event in session.events() if isinstance(event, ToolCallRecorded)][-1]
    assert recorded.is_error is True
    assert "timed out" in recorded.result


def _search_call(query: str = "review findings architecture core loop") -> ToolCall:
    return ToolCall(id="1", name="search_facts", arguments={"query": query})


class SearchScriptedClient:
    def __init__(self, turns: list[list[StreamEvent]], replies: list[str]) -> None:
        self._turns = turns
        self._replies = replies
        self.seen_tools: list[list[ToolSpec]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_tools.append(tools)
        if not tools:
            yield TextDelta(text=self._replies.pop(0))
            return
        yield from self._turns.pop(0)


def _seeded_session() -> Session:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    session.append(
        ToolCallRecorded(
            name="note", arguments={}, result="the run loop compiles context", is_error=False
        )
    )
    return session


def test_search_facts_returns_ids_with_reasons_from_the_sub_task() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(tool_call=_search_call()),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        ["1", "1", "[1] the note records how the core loop builds its prompt"],
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    recorded = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    ]
    assert recorded[0].is_error is False
    assert recorded[0].result.startswith("[1] the note records how the core loop builds its prompt")
    assert client.seen_tools[1] == []


def test_search_facts_inside_a_delegate_sends_no_tool_specs() -> None:
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(
                    tool_call=ToolCall(id="1", name="delegate", arguments={"question": "q"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c1", name="note", arguments={"content": "seed"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(id="c2", name="search_facts", arguments={"query": "seed"})
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [
                ToolCallReady(
                    tool_call=ToolCall(
                        id="c3", name="answer", arguments={"content": "ok", "citations": []}
                    )
                ),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        ["1", "1", "[1] seed is the match"],
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    child = [
        event
        for event in session.child("delegate/2").events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    ]
    assert child[0].result.startswith("[1] seed is the match")
    assert [] in client.seen_tools


def test_search_facts_without_relevant_facts_offers_the_fact_index() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(tool_call=_search_call()),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        ["none"],
    )

    LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG).execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    )
    assert recorded.is_error is False
    assert "no relevant facts found for" in recorded.result
    assert "[1] note(): the run loop compiles context" in recorded.result


def test_search_facts_with_empty_query_is_an_invalid_action() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    client = SearchScriptedClient(
        [
            [
                ToolCallReady(tool_call=_search_call("   ")),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ],
        [],
    )

    runner = LoopRunner(client, tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    )
    assert recorded.is_error is True
    assert runner.dispatch.invalid_action_attempts == 1


def test_llm_error_mid_search_records_a_failed_call_and_the_run_continues() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)

    class ExplodingSearch:
        def __init__(self) -> None:
            self.turns = [
                [
                    ToolCallReady(tool_call=_search_call()),
                    GenerationComplete(finish_reason="tool_calls"),
                ],
                answer_turn(),
            ]

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            if not tools:
                raise LLMError("connection lost")
            yield from self.turns.pop(0)

    runner = LoopRunner(ExplodingSearch(), tools, Bus(), session, 128_000, DEFAULT_LOOP_CONFIG)
    runner.execute()

    recorded = next(
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    )
    assert recorded.is_error is True
    assert "search failed" in recorded.result
    assert runner.error is None
    assert runner.iterations == 2


def test_cancelling_mid_search_cancels_the_run_without_recording_a_result() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    cancel = threading.Event()

    class CancelDuringSearch:
        def __init__(self) -> None:
            self.turns = [
                [
                    ToolCallReady(tool_call=_search_call()),
                    GenerationComplete(finish_reason="tool_calls"),
                ]
            ]

        def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
            if not tools:
                cancel.set()
                yield TextDelta(text="2")
                return
            yield from self.turns.pop(0)

    bus = Bus()
    subscriber = bus.subscribe()
    runner = LoopRunner(
        CancelDuringSearch(), tools, bus, session, 128_000, DEFAULT_LOOP_CONFIG, cancel
    )
    runner.execute()

    published = [next(subscriber) for _ in range(4)]
    assert RunCancelled() in published
    assert [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "search_facts"
    ] == []


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


def test_answer_spec_documents_verify() -> None:
    spec = RUNNER_ACTIONS["answer"].spec
    properties = spec.parameters["properties"]
    required = spec.parameters["required"]
    assert isinstance(properties, dict)
    assert isinstance(required, list)
    assert "verify" in properties
    assert "verify" not in required
    assert "exits 0" in spec.description


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
