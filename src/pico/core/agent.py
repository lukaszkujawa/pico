import threading

from pico.core.bus import Bus
from pico.core.errors import UnknownToolError
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
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallReady,
    ToolResult,
)


class Run:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        bus: Bus,
        messages: list[Message],
        cancel: threading.Event | None = None,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._bus = bus
        self.messages = list(messages)
        self._next_id = 0
        self.cancel = cancel if cancel is not None else threading.Event()

    def _new_id(self) -> str:
        stream_id = str(self._next_id)
        self._next_id += 1
        return stream_id

    def execute(self) -> None:
        self._bus.publish(RunStarted())
        try:
            while True:
                outcome = self._step()
                if outcome == "cancelled":
                    self._bus.publish(RunCancelled())
                    return
                if outcome == "done":
                    break
        except LLMError as error:
            self._bus.publish(ErrorOccurred(message=str(error)))
            self._bus.publish(RunFinished(error=str(error)))
            return
        self._bus.publish(RunFinished())

    def _step(self) -> str:
        text = ""
        tool_calls: list[ToolCall] = []
        text_id: str | None = None
        thinking_id: str | None = None
        cancelled = False

        for event in self._llm.stream(self.messages, self._tools.specs()):
            if self.cancel.is_set():
                cancelled = True
                break
            match event:
                case ThinkingDelta(text=chunk):
                    if thinking_id is None:
                        thinking_id = self._new_id()
                        self._bus.publish(AssistantThinkingStarted(id=thinking_id))
                    self._bus.publish(AssistantThinkingDelta(id=thinking_id, text=chunk))
                case TextDelta(text=chunk):
                    if text_id is None:
                        text_id = self._new_id()
                        self._bus.publish(AssistantTextStarted(id=text_id))
                    text += chunk
                    self._bus.publish(AssistantTextDelta(id=text_id, text=chunk))
                case ToolCallDelta(id=call_id, arguments_delta=arguments_delta):
                    self._bus.publish(
                        ToolCallArgumentsDelta(id=call_id, arguments_delta=arguments_delta)
                    )
                case ToolCallReady(tool_call=tool_call):
                    tool_calls.append(tool_call)
                case GenerationComplete():
                    pass

        if thinking_id is not None:
            self._bus.publish(AssistantThinkingFinished(id=thinking_id))
        if text_id is not None:
            self._bus.publish(AssistantTextFinished(id=text_id))

        if cancelled:
            return "cancelled"

        self.messages.append(
            Message(role=Role.ASSISTANT, content=text, tool_calls=tuple(tool_calls))
        )

        if not tool_calls:
            return "done"

        for call in tool_calls:
            self._run_tool_call(call)

        return "continue"

    def _run_tool_call(self, call: ToolCall) -> None:
        self._bus.publish(ToolCallStarted(id=call.id, name=call.name))
        try:
            output = self._tools.execute(call)
            is_error = False
        except UnknownToolError as error:
            output = str(error)
            is_error = True
        self._bus.publish(
            ToolCallFinished(id=call.id, tool_call=call, result=output, is_error=is_error)
        )
        self.messages.append(
            Message(
                role=Role.TOOL,
                tool_result=ToolResult(tool_call_id=call.id, content=output, is_error=is_error),
            )
        )
