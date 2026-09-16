import json
from collections.abc import Mapping

from textual.message import Message

from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BudgetExceeded,
    BusEvent,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)


class RunStartedMessage(Message):
    pass


class RunFinishedMessage(Message):
    def __init__(self, error: str | None) -> None:
        self.error = error
        super().__init__()


class RunCancelledMessage(Message):
    pass


class AssistantPaneCreate(Message):
    def __init__(self, pane_id: str) -> None:
        self.pane_id = pane_id
        super().__init__()


class AssistantPaneDelta(Message):
    def __init__(self, pane_id: str, text: str) -> None:
        self.pane_id = pane_id
        self.text = text
        super().__init__()


class AssistantPaneClose(Message):
    def __init__(self, pane_id: str) -> None:
        self.pane_id = pane_id
        super().__init__()


class ThinkingPaneCreate(Message):
    def __init__(self, pane_id: str) -> None:
        self.pane_id = pane_id
        super().__init__()


class ThinkingPaneDelta(Message):
    def __init__(self, pane_id: str, text: str) -> None:
        self.pane_id = pane_id
        self.text = text
        super().__init__()


class ThinkingPaneClose(Message):
    def __init__(self, pane_id: str) -> None:
        self.pane_id = pane_id
        super().__init__()


class ToolCallPaneCreate(Message):
    def __init__(self, pane_id: str, name: str, arguments: str) -> None:
        self.pane_id = pane_id
        self.name = name
        self.arguments = arguments
        super().__init__()


class ToolCallPaneClose(Message):
    def __init__(self, pane_id: str, result: str, is_error: bool) -> None:
        self.pane_id = pane_id
        self.result = result
        self.is_error = is_error
        super().__init__()


class AnswerPaneCreate(Message):
    def __init__(self, pane_id: str, content: str) -> None:
        self.pane_id = pane_id
        self.content = content
        super().__init__()


class GenerationCompletedMessage(Message):
    def __init__(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        super().__init__()


class ErrorMessage(Message):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__()


class UserInputSubmitted(Message):
    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


TuiMessage = (
    RunStartedMessage
    | RunFinishedMessage
    | RunCancelledMessage
    | AssistantPaneCreate
    | AssistantPaneDelta
    | AssistantPaneClose
    | ThinkingPaneCreate
    | ThinkingPaneDelta
    | ThinkingPaneClose
    | ToolCallPaneCreate
    | ToolCallPaneClose
    | AnswerPaneCreate
    | GenerationCompletedMessage
    | ErrorMessage
    | UserInputSubmitted
)


def format_arguments(arguments: Mapping[str, object]) -> str:
    return json.dumps(arguments, separators=(", ", ": "))


def translate(event: BusEvent) -> TuiMessage | None:
    match event:
        case RunStarted():
            return RunStartedMessage()
        case RunFinished(error=error):
            return RunFinishedMessage(error=error)
        case RunCancelled():
            return RunCancelledMessage()
        case AssistantTextStarted(id=pane_id):
            return AssistantPaneCreate(pane_id=pane_id)
        case AssistantTextDelta(id=pane_id, text=text):
            return AssistantPaneDelta(pane_id=pane_id, text=text)
        case AssistantTextFinished(id=pane_id):
            return AssistantPaneClose(pane_id=pane_id)
        case AssistantThinkingStarted(id=pane_id):
            return ThinkingPaneCreate(pane_id=pane_id)
        case AssistantThinkingDelta(id=pane_id, text=text):
            return ThinkingPaneDelta(pane_id=pane_id, text=text)
        case AssistantThinkingFinished(id=pane_id):
            return ThinkingPaneClose(pane_id=pane_id)
        case ToolCallStarted(id=pane_id, name=name, arguments=arguments):
            return ToolCallPaneCreate(
                pane_id=pane_id, name=name, arguments=format_arguments(arguments)
            )
        case ToolCallFinished(id=pane_id, tool_call=tool_call, result=result, is_error=False) if (
            tool_call.name == "answer"
        ):
            return AnswerPaneCreate(pane_id=pane_id, content=result)
        case ToolCallFinished(id=pane_id, result=result, is_error=is_error):
            return ToolCallPaneClose(pane_id=pane_id, result=result, is_error=is_error)
        case GenerationCompleted(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens):
            return GenerationCompletedMessage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
            )
        case ErrorOccurred(message=message):
            return ErrorMessage(message=message)
        case BudgetExceeded():
            return None
