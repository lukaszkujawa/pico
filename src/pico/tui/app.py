import threading

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from pico.core.bus import Bus
from pico.tui.messages import (
    ErrorMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ThinkingBoxClose,
    ThinkingBoxCreate,
    ThinkingBoxDelta,
    ToolCallBoxClose,
    ToolCallBoxCreate,
    ToolCallBoxDelta,
    translate,
)
from pico.tui.theme import PICO_THEME
from pico.tui.widgets import ThinkingBox, ToolCallBox


class PicoApp(App[None]):
    def __init__(self, bus: Bus) -> None:
        super().__init__()
        self._bus = bus
        self._thinking_boxes: dict[str, ThinkingBox] = {}
        self._tool_call_boxes: dict[str, ToolCallBox] = {}

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="conversation")

    def on_mount(self) -> None:
        self.register_theme(PICO_THEME.to_textual())
        self.theme = PICO_THEME.name
        threading.Thread(target=self._consume_bus, daemon=True).start()

    def _consume_bus(self) -> None:
        for event in self._bus.subscribe():
            message = translate(event)
            if message is not None:
                self.post_message(message)

    def on_thinking_box_create(self, message: ThinkingBoxCreate) -> None:
        box = ThinkingBox(box_id=message.box_id)
        self._thinking_boxes[message.box_id] = box
        self.query_one("#conversation", VerticalScroll).mount(box)

    def on_thinking_box_delta(self, message: ThinkingBoxDelta) -> None:
        self._thinking_boxes[message.box_id].append_delta(message.text)

    def on_thinking_box_close(self, message: ThinkingBoxClose) -> None:
        self._thinking_boxes[message.box_id].finish()

    def on_tool_call_box_create(self, message: ToolCallBoxCreate) -> None:
        box = ToolCallBox(box_id=message.box_id, name=message.name)
        self._tool_call_boxes[message.box_id] = box
        self.query_one("#conversation", VerticalScroll).mount(box)

    def on_tool_call_box_delta(self, message: ToolCallBoxDelta) -> None:
        self._tool_call_boxes[message.box_id].append_delta(message.text)

    def on_tool_call_box_close(self, message: ToolCallBoxClose) -> None:
        self._tool_call_boxes[message.box_id].finish(is_error=message.is_error)

    def on_run_started(self, message: RunStartedMessage) -> None:
        pass

    def on_run_finished(self, message: RunFinishedMessage) -> None:
        pass

    def on_error_message(self, message: ErrorMessage) -> None:
        self.notify(message.message, severity="error")
