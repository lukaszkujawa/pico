import threading
from typing import Literal

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Rule, Static

from pico.core.bus import Bus
from pico.tui.messages import (
    AssistantPaneClose,
    AssistantPaneCreate,
    AssistantPaneDelta,
    ErrorMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ToolCallPaneClose,
    ToolCallPaneCreate,
    ToolCallPaneDelta,
    UserInputSubmitted,
    translate,
)
from pico.tui.theme import PICO_THEME, Theme
from pico.tui.widgets import AssistantPane, ErrorPane, ToolCallPane

RunStatus = Literal["idle", "running", "error"]


class StatusHeader(Static):
    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="status-header")
        self._theme = theme
        self.status: RunStatus = "idle"
        self.styles.background = theme.surface
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def set_status(self, status: RunStatus) -> None:
        self.status = status
        self.refresh()

    def render(self) -> Text:
        color = {
            "idle": self._theme.idle,
            "running": self._theme.running,
            "error": self._theme.error,
        }[self.status]
        return Text.assemble(
            ("pico", f"bold {self._theme.primary}"),
            "  ",
            (self.status, f"bold {color}"),
        )


class InputBar(Horizontal):
    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="input-bar")
        self._theme = theme
        self.styles.background = theme.input_bar_bg
        self.styles.height = 1

    def compose(self) -> ComposeResult:
        prompt = Static(">", id="input-prompt")
        prompt.styles.color = self._theme.input_prompt
        prompt.styles.width = 2
        yield prompt
        yield Input(placeholder="Type a message...", id="user-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value
        event.input.value = ""
        self.post_message(UserInputSubmitted(text=text))


class PicoApp(App[None]):
    CSS = """
    #conversation {
        height: 1fr;
    }
    #input-bar {
        dock: bottom;
        height: 1;
    }
    """

    def __init__(self, bus: Bus) -> None:
        super().__init__()
        self._bus = bus
        self._assistant_panes: dict[str, AssistantPane] = {}
        self._tool_call_panes: dict[str, ToolCallPane] = {}

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        yield VerticalScroll(id="conversation")
        yield Rule()
        yield InputBar()
        yield Rule()

    def on_mount(self) -> None:
        self.register_theme(PICO_THEME.to_textual())
        self.theme = PICO_THEME.name
        threading.Thread(target=self._consume_bus, daemon=True).start()

    def _consume_bus(self) -> None:
        for event in self._bus.subscribe():
            message = translate(event)
            if message is not None:
                self.post_message(message)

    def on_assistant_pane_create(self, message: AssistantPaneCreate) -> None:
        pane = AssistantPane(pane_id=message.pane_id)
        self._assistant_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_assistant_pane_delta(self, message: AssistantPaneDelta) -> None:
        self._assistant_panes[message.pane_id].append_delta(message.text)

    def on_assistant_pane_close(self, message: AssistantPaneClose) -> None:
        self._assistant_panes[message.pane_id].finish()

    def on_tool_call_pane_create(self, message: ToolCallPaneCreate) -> None:
        pane = ToolCallPane(pane_id=message.pane_id, name=message.name)
        self._tool_call_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_tool_call_pane_delta(self, message: ToolCallPaneDelta) -> None:
        self._tool_call_panes[message.pane_id].append_delta(message.text)

    def on_tool_call_pane_close(self, message: ToolCallPaneClose) -> None:
        self._tool_call_panes[message.pane_id].finish(is_error=message.is_error)

    def on_run_started_message(self, message: RunStartedMessage) -> None:
        self.query_one(StatusHeader).set_status("running")

    def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self.query_one(StatusHeader).set_status("error" if message.error else "idle")

    def on_error_message(self, message: ErrorMessage) -> None:
        self.query_one(StatusHeader).set_status("error")
        self.query_one("#conversation", VerticalScroll).mount(ErrorPane(message.message))

    def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
        pass
