import queue
import threading
from typing import ClassVar, Literal, Protocol

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Rule, Static

from pico.core.bus import Bus
from pico.tui.messages import (
    AssistantPaneClose,
    AssistantPaneCreate,
    AssistantPaneDelta,
    ErrorMessage,
    RunCancelledMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ThinkingPaneClose,
    ThinkingPaneCreate,
    ThinkingPaneDelta,
    ToolCallPaneClose,
    ToolCallPaneCreate,
    ToolCallPaneDelta,
    UserInputSubmitted,
    translate,
)
from pico.tui.theme import PICO_THEME, Theme
from pico.tui.widgets import (
    AssistantPane,
    ErrorPane,
    ThinkingPane,
    ToolCallPane,
    UserPane,
    WaitingIndicator,
)

RunStatus = Literal["idle", "running", "error"]


class CancelHandle(Protocol):
    def trigger(self) -> None: ...


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
        self.styles.background = theme.background
        self.styles.height = 1

    def compose(self) -> ComposeResult:
        prompt = Static(">", id="input-prompt")
        prompt.styles.color = self._theme.input_prompt
        prompt.styles.width = 2
        yield prompt
        text_input = Input(placeholder="Type a message...", id="user-input")
        text_input.add_class("-textual-compact")
        yield text_input

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
    #footer {
        dock: bottom;
        height: 2;
    }
    #input-bar {
        height: 1;
    }
    #user-input, #user-input:focus {
        background: $background;
        background-tint: $background 0%;
    }
    Rule {
        margin: 0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [("escape", "cancel_run", "Cancel")]

    def __init__(
        self,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: CancelHandle | None = None,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._input_queue = input_queue
        self._cancel_handle = cancel_handle
        self._run_in_flight = False
        self._assistant_panes: dict[str, AssistantPane] = {}
        self._thinking_panes: dict[str, ThinkingPane] = {}
        self._tool_call_panes: dict[str, ToolCallPane] = {}

    def compose(self) -> ComposeResult:
        yield StatusHeader()
        with VerticalScroll(id="conversation"):
            yield WaitingIndicator()
        yield Rule()
        with Vertical(id="footer"):
            yield InputBar()
            yield Rule()

    def on_mount(self) -> None:
        self.register_theme(PICO_THEME.to_textual())
        self.theme = PICO_THEME.name
        self.query_one("#user-input", Input).focus()
        threading.Thread(target=self._consume_bus, daemon=True).start()

    def _consume_bus(self) -> None:
        for event in self._bus.subscribe():
            message = translate(event)
            if message is not None:
                self.post_message(message)

    def on_assistant_pane_create(self, message: AssistantPaneCreate) -> None:
        self.query_one(WaitingIndicator).stop()
        pane = AssistantPane(pane_id=message.pane_id)
        self._assistant_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_assistant_pane_delta(self, message: AssistantPaneDelta) -> None:
        self._assistant_panes[message.pane_id].append_delta(message.text)

    def on_assistant_pane_close(self, message: AssistantPaneClose) -> None:
        self._assistant_panes[message.pane_id].finish()

    def on_thinking_pane_create(self, message: ThinkingPaneCreate) -> None:
        self.query_one(WaitingIndicator).stop()
        pane = ThinkingPane(pane_id=message.pane_id)
        self._thinking_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_thinking_pane_delta(self, message: ThinkingPaneDelta) -> None:
        self._thinking_panes[message.pane_id].append_delta(message.text)

    def on_thinking_pane_close(self, message: ThinkingPaneClose) -> None:
        self._thinking_panes[message.pane_id].finish()

    def on_tool_call_pane_create(self, message: ToolCallPaneCreate) -> None:
        pane = ToolCallPane(pane_id=message.pane_id, name=message.name)
        self._tool_call_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_tool_call_pane_delta(self, message: ToolCallPaneDelta) -> None:
        self._tool_call_panes[message.pane_id].append_delta(message.text)

    def on_tool_call_pane_close(self, message: ToolCallPaneClose) -> None:
        self._tool_call_panes[message.pane_id].finish(is_error=message.is_error)

    def on_run_started_message(self, message: RunStartedMessage) -> None:
        self._run_in_flight = True
        self.query_one(StatusHeader).set_status("running")

    def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self._run_in_flight = False
        self.query_one(WaitingIndicator).stop()
        self.query_one(StatusHeader).set_status("error" if message.error else "idle")

    def on_run_cancelled_message(self, message: RunCancelledMessage) -> None:
        self._run_in_flight = False
        self.query_one(WaitingIndicator).stop()
        self.query_one(StatusHeader).set_status("idle")

    def action_cancel_run(self) -> None:
        if self._run_in_flight and self._cancel_handle is not None:
            self._cancel_handle.trigger()

    def on_error_message(self, message: ErrorMessage) -> None:
        self._run_in_flight = False
        self.query_one(WaitingIndicator).stop()
        self.query_one(StatusHeader).set_status("error")
        self.query_one("#conversation", VerticalScroll).mount(ErrorPane(message.message))

    def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
        text = message.text.strip()
        if not text:
            return
        conversation = self.query_one("#conversation", VerticalScroll)
        conversation.mount(UserPane(text=message.text))
        indicator = self.query_one(WaitingIndicator)
        conversation.move_child(indicator, after=-1)
        indicator.start()
        self._input_queue.put(text)
