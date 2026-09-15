from pico.core.bus import Bus
from pico.core.errors import UnknownToolError
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
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    Message,
    Role,
    TextDelta,
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
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._bus = bus
        self._messages = list(messages)

    def execute(self) -> None:
        self._bus.publish(RunStarted())
        try:
            while self._step():
                pass
        except LLMError as error:
            self._bus.publish(ErrorOccurred(message=str(error)))
            self._bus.publish(RunFinished(error=str(error)))
            return
        self._bus.publish(RunFinished())

    def _step(self) -> bool:
        text = ""
        tool_calls: list[ToolCall] = []
        text_open = False

        for event in self._llm.stream(self._messages, self._tools.specs()):
            match event:
                case TextDelta(text=chunk):
                    if not text_open:
                        self._bus.publish(AssistantTextStarted(id="0"))
                        text_open = True
                    text += chunk
                    self._bus.publish(AssistantTextDelta(id="0", text=chunk))
                case ToolCallDelta(id=call_id, arguments_delta=arguments_delta):
                    self._bus.publish(
                        ToolCallArgumentsDelta(id=call_id, arguments_delta=arguments_delta)
                    )
                case ToolCallReady(tool_call=tool_call):
                    tool_calls.append(tool_call)
                case GenerationComplete():
                    pass

        if text_open:
            self._bus.publish(AssistantTextFinished(id="0"))

        self._messages.append(
            Message(role=Role.ASSISTANT, content=text, tool_calls=tuple(tool_calls))
        )

        if not tool_calls:
            return False

        for call in tool_calls:
            self._run_tool_call(call)

        return True

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
        self._messages.append(
            Message(
                role=Role.TOOL,
                tool_result=ToolResult(tool_call_id=call.id, content=output, is_error=is_error),
            )
        )
