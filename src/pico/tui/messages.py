from textual.message import Message

from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    BusEvent,
    ErrorOccurred,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)


class RunStartedMessage(Message):
    pass


class RunFinishedMessage(Message):
    def __init__(self, error: str | None) -> None:
        self.error = error
        super().__init__()


class ThinkingBoxCreate(Message):
    def __init__(self, box_id: str) -> None:
        self.box_id = box_id
        super().__init__()


class ThinkingBoxDelta(Message):
    def __init__(self, box_id: str, text: str) -> None:
        self.box_id = box_id
        self.text = text
        super().__init__()


class ThinkingBoxClose(Message):
    def __init__(self, box_id: str) -> None:
        self.box_id = box_id
        super().__init__()


class ToolCallBoxCreate(Message):
    def __init__(self, box_id: str, name: str) -> None:
        self.box_id = box_id
        self.name = name
        super().__init__()


class ToolCallBoxDelta(Message):
    def __init__(self, box_id: str, text: str) -> None:
        self.box_id = box_id
        self.text = text
        super().__init__()


class ToolCallBoxClose(Message):
    def __init__(self, box_id: str, result: str, is_error: bool) -> None:
        self.box_id = box_id
        self.result = result
        self.is_error = is_error
        super().__init__()


class ErrorMessage(Message):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__()


TuiMessage = (
    RunStartedMessage
    | RunFinishedMessage
    | ThinkingBoxCreate
    | ThinkingBoxDelta
    | ThinkingBoxClose
    | ToolCallBoxCreate
    | ToolCallBoxDelta
    | ToolCallBoxClose
    | ErrorMessage
)


def translate(event: BusEvent) -> TuiMessage | None:
    match event:
        case RunStarted():
            return RunStartedMessage()
        case RunFinished(error=error):
            return RunFinishedMessage(error=error)
        case AssistantTextStarted(id=box_id):
            return ThinkingBoxCreate(box_id=box_id)
        case AssistantTextDelta(id=box_id, text=text):
            return ThinkingBoxDelta(box_id=box_id, text=text)
        case AssistantTextFinished(id=box_id):
            return ThinkingBoxClose(box_id=box_id)
        case ToolCallStarted(id=box_id, name=name):
            return ToolCallBoxCreate(box_id=box_id, name=name)
        case ToolCallArgumentsDelta(id=box_id, arguments_delta=arguments_delta):
            return ToolCallBoxDelta(box_id=box_id, text=arguments_delta)
        case ToolCallFinished(id=box_id, result=result, is_error=is_error):
            return ToolCallBoxClose(box_id=box_id, result=result, is_error=is_error)
        case ErrorOccurred(message=message):
            return ErrorMessage(message=message)
