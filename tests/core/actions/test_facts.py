import threading
from collections.abc import Iterator

import pytest

from pico.core.actions import (
    InvalidActionError,
    fact_recall_tool,
    note_tool,
    register_actions,
)
from pico.core.bus import Bus
from pico.core.errors import ToolError
from pico.core.events import (
    RunCancelled,
)
from pico.core.ledger import facts
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.runner import LoopRunner
from pico.core.loop.state import Answered, Failed
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
    Session,
    ToolCallRecorded,
    UserMessageRecorded,
    connect,
)
from tests.core.loop_fixtures import (
    ScriptedClient,
    answer_turn,
    make_session,
)
from tests.llm_fakes import NoModels


def _session_with_fact(content: str) -> tuple[Session, int]:
    session = Session(connect(":memory:"), "s1")
    session.append(ToolCallRecorded(name="shell", arguments={}, result=content, is_error=False))
    return session, 1


def test_read_fact_returns_original_content() -> None:
    session, fact_id = _session_with_fact("x" * 5000)

    result = fact_recall_tool(session).execute({"id": fact_id})

    assert result == "x" * 5000


def test_read_fact_unknown_id_raises_tool_error_naming_the_id() -> None:
    session, _ = _session_with_fact("hello")

    with pytest.raises(ToolError, match="99"):
        fact_recall_tool(session).execute({"id": 99})


def test_read_fact_error_result_is_not_a_fact() -> None:
    session = Session(connect(":memory:"), "s1")
    session.append(ToolCallRecorded(name="shell", arguments={}, result="boom", is_error=True))

    with pytest.raises(ToolError):
        fact_recall_tool(session).execute({"id": 1})


def test_read_fact_non_integer_id_raises_invalid_action_error() -> None:
    session, _ = _session_with_fact("hello")

    with pytest.raises(InvalidActionError, match="must be a int"):
        fact_recall_tool(session).execute({"id": "1"})


def test_read_fact_missing_id_raises_invalid_action_error() -> None:
    session, _ = _session_with_fact("hello")

    with pytest.raises(InvalidActionError, match="missing required field"):
        fact_recall_tool(session).execute({})


def test_note_records_its_content_as_the_result() -> None:
    assert note_tool().execute({"content": "the Bus drops subscribers on error"}) == (
        "the Bus drops subscribers on error"
    )


def test_note_empty_content_raises_invalid_action_error() -> None:
    with pytest.raises(InvalidActionError, match="must not be empty"):
        note_tool().execute({"content": "   "})


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

    handle = next(
        message.tool_result
        for message in client.seen_messages[0]
        if message.tool_result is not None
    )
    assert "fact 1 shell() truncated" in handle.content
    assert "call read_fact(1) for the full content" in handle.content

    recalled = [
        event
        for event in session.events()
        if isinstance(event, ToolCallRecorded) and event.name == "read_fact"
    ]
    assert recalled[0].result == content
    assert recalled[0].is_error is False
    assert runner.state == Answered("it is needles")


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


def _search_call(query: str = "review findings architecture core loop") -> ToolCall:
    return ToolCall(id="1", name="search_facts", arguments={"query": query})


class SearchScriptedClient(NoModels):
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

    class ExplodingSearch(NoModels):
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
    assert not isinstance(runner.state, Failed)
    assert runner.iterations == 2


def test_cancelling_mid_search_cancels_the_run_without_recording_a_result() -> None:
    session = _seeded_session()
    tools = ToolRegistry()
    register_actions(tools, session)
    cancel = threading.Event()

    class CancelDuringSearch(NoModels):
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
