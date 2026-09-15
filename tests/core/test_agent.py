from collections.abc import Iterator

from pico.core.agent import Run
from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    ErrorOccurred,
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
