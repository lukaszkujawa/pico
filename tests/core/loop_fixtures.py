import threading
from collections.abc import Iterator, Mapping

from pico.core.actions import MAX_DELEGATE_DEPTH, register_actions
from pico.core.errors import ToolError
from pico.core.events import (
    BusEvent,
    RunFinished,
)
from pico.core.loop.runner import LoopRunner
from pico.core.tools import Tool, ToolRegistry
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)
from pico.session import (
    Session,
    UserMessageRecorded,
    connect,
)
from tests.llm_fakes import NoModels


class ScriptedClient(NoModels):
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self.seen_messages: list[list[Message]] = []
        self.seen_tools: list[list[ToolSpec]] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(messages)
        self.seen_tools.append(tools)
        yield from self._turns.pop(0)


class FailingClient(NoModels):
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        raise LLMError("connection lost")
        yield


class CancellingClient(NoModels):
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


def echo_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="echo", description="echo", parameters={"type": "object"}),
            execute=lambda args: str(args.get("text", "")),
        )
    )
    return registry


def failing_registry() -> ToolRegistry:
    def boom(arguments: Mapping[str, object]) -> str:
        raise ToolError("could not read /nope: no such file")

    registry = ToolRegistry()
    registry.register(
        Tool(
            spec=ToolSpec(name="boom", description="boom", parameters={"type": "object"}),
            execute=boom,
        )
    )
    return registry


def make_session(session_id: str = "s1") -> Session:
    conn = connect(":memory:")
    return Session(conn, session_id)


class RecordingClient(NoModels):
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self.seen_messages: list[list[Message]] = []
        self.seen_specs: list[list[ToolSpec]] = []
        self.seen_ratios: list[float] = []
        self.runner: LoopRunner | None = None

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.seen_messages.append(messages)
        self.seen_specs.append(tools)
        if self.runner is not None:
            self.seen_ratios.append(self.runner.chars_per_token)
        yield from self._turns.pop(0)


def stop_turn() -> list[StreamEvent]:
    return [TextDelta(text="hello"), GenerationComplete(finish_reason="stop")]


def answer_turn(content: str = "done") -> list[StreamEvent]:
    return [
        ToolCallReady(
            tool_call=ToolCall(
                id="a1", name="answer", arguments={"content": content, "citations": []}
            )
        ),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def delegate_turn(question: str = "q") -> list[list[StreamEvent]]:
    return [
        [
            ToolCallReady(
                tool_call=ToolCall(id="1", name="delegate", arguments={"question": question})
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        [
            ToolCallReady(
                tool_call=ToolCall(
                    id="c1", name="answer", arguments={"content": "done", "citations": []}
                )
            ),
            GenerationComplete(finish_reason="tool_calls"),
        ],
        [TextDelta(text="ok"), GenerationComplete(finish_reason="stop")],
    ]


def drain_until_run_finished(subscriber: Iterator[BusEvent]) -> list[BusEvent]:
    events: list[BusEvent] = []
    for event in subscriber:
        events.append(event)
        if isinstance(event, RunFinished):
            return events
    return events


def set_plan_turn(steps: list[str]) -> list[StreamEvent]:
    return [
        ToolCallReady(tool_call=ToolCall(id="p1", name="set_plan", arguments={"steps": steps})),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def text_turn(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text), GenerationComplete(finish_reason="stop")]


def repeat_turns(count: int) -> list[list[StreamEvent]]:
    return [
        [
            ToolCallReady(tool_call=ToolCall(id=str(i), name="echo", arguments={"text": "same"})),
            GenerationComplete(finish_reason="tool_calls"),
        ]
        for i in range(count)
    ]


def note_turn(index: int) -> list[StreamEvent]:
    return [
        ToolCallReady(
            tool_call=ToolCall(
                id=str(index), name="note", arguments={"content": f"finding {index}"}
            )
        ),
        GenerationComplete(finish_reason="tool_calls"),
    ]


def decision_session() -> tuple[Session, ToolRegistry]:
    session = make_session()
    session.append(UserMessageRecorded(content="survey the repository"))
    registry = ToolRegistry()
    register_actions(registry, session, depth=MAX_DELEGATE_DEPTH)
    return session, registry


def decision_demands(client: ScriptedClient) -> list[str]:
    return [
        messages[-1].content
        for messages in client.seen_messages
        if messages[-1].role is Role.USER and "decision required" in messages[-1].content
    ]
