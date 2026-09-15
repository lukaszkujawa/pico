import threading
from collections.abc import Iterator

from pico.core.agent import Run
from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.core.tools import Tool, ToolRegistry
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
    ToolSpec,
)


class ScriptedClient:
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        yield from self._turns.pop(0)


class FailingClient:
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        raise LLMError("connection lost")
        yield


class CancellingClient:
    def __init__(
        self, events: list[StreamEvent], cancel: threading.Event, cancel_after: int
    ) -> None:
        self._events = events
        self._cancel = cancel
        self._cancel_after = cancel_after

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        for index, event in enumerate(self._events):
            if index == self._cancel_after:
                self._cancel.set()
            yield event


def _echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="echo", description="echo", parameters={"type": "object"}),
            execute=lambda args: str(args.get("text", "")),
        )
    )
    return registry


def test_plain_text_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    client = ScriptedClient(
        [
            [
                TextDelta(text="hello "),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="hello "),
        AssistantTextDelta(id="0", text="world"),
        AssistantTextFinished(id="0"),
        RunFinished(),
    ]


def test_thinking_then_text_published_in_order_with_shared_ids() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    client = ScriptedClient(
        [
            [
                ThinkingDelta(text="pondering "),
                ThinkingDelta(text="more"),
                TextDelta(text="hello "),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(10)]
    assert events == [
        RunStarted(),
        AssistantThinkingStarted(id="0"),
        AssistantThinkingDelta(id="0", text="pondering "),
        AssistantThinkingDelta(id="0", text="more"),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="hello "),
        AssistantTextDelta(id="1", text="world"),
        AssistantThinkingFinished(id="0"),
        AssistantTextFinished(id="1"),
        RunFinished(),
    ]


def test_text_only_step_publishes_no_thinking_events() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    client = ScriptedClient(
        [
            [TextDelta(text="hi"), GenerationComplete(finish_reason="stop")],
        ]
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="hi"),
        AssistantTextFinished(id="0"),
        RunFinished(),
    ]
    assert not any(isinstance(event, AssistantThinkingStarted) for event in events)


def test_stream_ids_unique_across_two_turns_on_same_run() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    messages = [Message(role=Role.USER, content="hi")]
    client = ScriptedClient(
        [
            [
                ThinkingDelta(text="first thought"),
                TextDelta(text="first answer"),
                GenerationComplete(finish_reason="stop"),
            ],
            [
                ThinkingDelta(text="second thought"),
                TextDelta(text="second answer"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )

    run = Run(client, _echo_registry(), bus, messages)
    run.execute()

    first_events = [next(subscriber) for _ in range(7)]
    started_ids_first = [
        event.id
        for event in first_events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted)
    ]
    assert started_ids_first == ["0", "1"]

    run.execute()

    second_events = [next(subscriber) for _ in range(7)]
    started_ids_second = [
        event.id
        for event in second_events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted)
    ]
    assert started_ids_second == ["2", "3"]
    assert set(started_ids_first).isdisjoint(started_ids_second)


def test_single_tool_call_round_trip() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [TextDelta(text="done"), GenerationComplete(finish_reason="stop")],
        ]
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(7)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="echo"),
        ToolCallFinished(id="1", tool_call=call, result="hi", is_error=False),
        AssistantTextStarted(id="0"),
        AssistantTextDelta(id="0", text="done"),
        AssistantTextFinished(id="0"),
        RunFinished(),
    ]


def test_tool_call_arguments_delta_forwarded() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    call = ToolCall(id="1", name="echo", arguments={"text": "hi"})
    client = ScriptedClient(
        [
            [
                ToolCallDelta(id="1", name="echo", arguments_delta='{"text":'),
                ToolCallReady(tool_call=call),
                GenerationComplete(finish_reason="tool_calls"),
            ],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(5)]
    assert events == [
        RunStarted(),
        ToolCallArgumentsDelta(id="1", arguments_delta='{"text":'),
        ToolCallStarted(id="1", name="echo"),
        ToolCallFinished(id="1", tool_call=call, result="hi", is_error=False),
        RunFinished(),
    ]


def test_unknown_tool_call_surfaced_as_tool_error() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    call = ToolCall(id="1", name="missing", arguments={})
    client = ScriptedClient(
        [
            [ToolCallReady(tool_call=call), GenerationComplete(finish_reason="tool_calls")],
            [GenerationComplete(finish_reason="stop")],
        ]
    )

    run = Run(client, ToolRegistry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(4)]
    assert events == [
        RunStarted(),
        ToolCallStarted(id="1", name="missing"),
        ToolCallFinished(id="1", tool_call=call, result="missing", is_error=True),
        RunFinished(),
    ]


def test_llm_error_surfaced_as_error_occurred() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    client = FailingClient()

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")])
    run.execute()

    events = [next(subscriber) for _ in range(3)]
    assert events == [
        RunStarted(),
        ErrorOccurred(message="connection lost"),
        RunFinished(error="connection lost"),
    ]


def test_cancel_set_before_streaming_stops_immediately() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    cancel = threading.Event()
    cancel.set()
    client = CancellingClient(
        events=[TextDelta(text="hello"), GenerationComplete(finish_reason="stop")],
        cancel=cancel,
        cancel_after=-1,
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")], cancel)
    run.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]


def test_cancel_mid_stream_stops_consuming_and_closes_open_panes() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    cancel = threading.Event()
    client = CancellingClient(
        events=[
            ThinkingDelta(text="pondering"),
            TextDelta(text="hel"),
            TextDelta(text="lo"),
            GenerationComplete(finish_reason="stop"),
        ],
        cancel=cancel,
        cancel_after=2,
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")], cancel)
    run.execute()

    events = [next(subscriber) for _ in range(6)]
    assert events == [
        RunStarted(),
        AssistantThinkingStarted(id="0"),
        AssistantThinkingDelta(id="0", text="pondering"),
        AssistantTextStarted(id="1"),
        AssistantTextDelta(id="1", text="hel"),
        AssistantThinkingFinished(id="0"),
    ]
    assert next(subscriber) == AssistantTextFinished(id="1")
    assert next(subscriber) == RunCancelled()


def test_cancelled_turn_does_not_publish_run_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    cancel = threading.Event()
    client = CancellingClient(
        events=[TextDelta(text="hi"), GenerationComplete(finish_reason="stop")],
        cancel=cancel,
        cancel_after=0,
    )

    run = Run(client, _echo_registry(), bus, [Message(role=Role.USER, content="hi")], cancel)
    run.execute()

    events = [next(subscriber) for _ in range(2)]
    assert events == [RunStarted(), RunCancelled()]
    assert not any(isinstance(event, RunFinished) for event in events)
