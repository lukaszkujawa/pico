import queue
import threading
from typing import ClassVar, Protocol

from textual import events
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Rule, Static, TextArea

from pico.core.bus import Bus
from pico.tui.messages import (
    AnswerPaneCreate,
    AssistantPaneClose,
    AssistantPaneCreate,
    AssistantPaneDelta,
    ErrorMessage,
    GenerationCompletedMessage,
    RunCancelledMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ThinkingPaneClose,
    ThinkingPaneCreate,
    ThinkingPaneDelta,
    ToolCallPaneClose,
    ToolCallPaneCreate,
    UserInputSubmitted,
    translate,
)
from pico.tui.theme import PICO_THEME, Theme
from pico.tui.widgets import (
    AnswerPane,
    AssistantPane,
    ErrorPane,
    Splash,
    StatusLine,
    ThinkingPane,
    ToolCallPane,
    UserPane,
)


class CancelHandle(Protocol):
    def trigger(self) -> None: ...


class SessionHandle(Protocol):
    @property
    def session_id(self) -> str: ...

    def start_new(self) -> None: ...


NEWLINE_KEYS = {"ctrl+j", "shift+enter"}


class ChatInput(TextArea):
    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text
            self.clear()
            self.post_message(UserInputSubmitted(text=text))
            return
        if event.key in NEWLINE_KEYS:
            event.key = "enter"
        await super()._on_key(event)


class InputBar(Horizontal):
    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="input-bar")
        self._theme = theme
        self.styles.background = theme.background

    def compose(self) -> ComposeResult:
        prompt = Static(">", id="input-prompt")
        prompt.styles.color = self._theme.input_prompt
        prompt.styles.width = 2
        yield prompt
        text_input = ChatInput(
            placeholder="Type a message...",
            id="user-input",
            show_line_numbers=False,
            soft_wrap=True,
        )
        yield text_input


class PicoApp(App[None]):
    CSS = """
    #conversation {
        height: 1fr;
    }
    #footer {
        dock: bottom;
        height: auto;
    }
    #input-bar {
        height: auto;
    }
    #status-line {
        height: auto;
        padding: 0 1;
    }
    #status-line Static {
        width: auto;
        height: 1;
    }
    #user-input {
        height: auto;
        max-height: 10;
        background: $background;
        border: none;
        padding: 0;
    }
    #user-input:focus {
        background: $background;
        border: none;
    }
    Rule {
        margin: 0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        ("escape", "cancel_run", "Cancel"),
        ("ctrl+n", "new_session", "New conversation"),
    ]

    def __init__(
        self,
        bus: Bus,
        input_queue: "queue.Queue[str]",
        cancel_handle: CancelHandle | None = None,
        session_handle: SessionHandle | None = None,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._input_queue = input_queue
        self._cancel_handle = cancel_handle
        self._session_handle = session_handle
        self._run_in_flight = False
        self._assistant_panes: dict[str, AssistantPane] = {}
        self._thinking_panes: dict[str, ThinkingPane] = {}
        self._tool_call_panes: dict[str, ToolCallPane] = {}
        self._queued_user_panes: list[UserPane] = []
        self._fact_count = 0

    def _session_id(self) -> str:
        return "" if self._session_handle is None else self._session_handle.session_id

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="conversation"):
            yield Splash(self._session_id())
            yield StatusLine()
        yield Rule()
        with Vertical(id="footer"):
            yield InputBar()
            yield Rule()

    def on_mount(self) -> None:
        self.register_theme(PICO_THEME.to_textual())
        self.theme = PICO_THEME.name
        self.query_one("#user-input", ChatInput).focus()
        self.query_one("#conversation", VerticalScroll).anchor()
        threading.Thread(target=self._consume_bus, daemon=True).start()

    def on_click(self, event: events.Click) -> None:
        text_input = self.query_one("#user-input", ChatInput)
        if event.widget is not text_input:
            text_input.focus()

    def _consume_bus(self) -> None:
        for event in self._bus.subscribe():
            message = translate(event)
            if message is not None:
                self.post_message(message)

    def _stop_status(self) -> None:
        self.query_one(StatusLine).stop()

    def _stop_spinner(self) -> None:
        self.query_one(StatusLine).stop_spinner()

    def on_assistant_pane_create(self, message: AssistantPaneCreate) -> None:
        self._stop_spinner()
        pane = AssistantPane(pane_id=message.pane_id)
        self._assistant_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_assistant_pane_delta(self, message: AssistantPaneDelta) -> None:
        self._assistant_panes[message.pane_id].append_delta(message.text)
        self.query_one(StatusLine).counter.estimate(message.text)

    def on_assistant_pane_close(self, message: AssistantPaneClose) -> None:
        self._assistant_panes[message.pane_id].finish()

    def on_thinking_pane_create(self, message: ThinkingPaneCreate) -> None:
        self._stop_spinner()
        pane = ThinkingPane(pane_id=message.pane_id)
        self._thinking_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_thinking_pane_delta(self, message: ThinkingPaneDelta) -> None:
        self._thinking_panes[message.pane_id].append_delta(message.text)
        self.query_one(StatusLine).counter.estimate(message.text)

    def on_thinking_pane_close(self, message: ThinkingPaneClose) -> None:
        self._thinking_panes[message.pane_id].finish()

    def on_tool_call_pane_create(self, message: ToolCallPaneCreate) -> None:
        pane = ToolCallPane(pane_id=message.pane_id, name=message.name, arguments=message.arguments)
        self._tool_call_panes[message.pane_id] = pane
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_tool_call_pane_close(self, message: ToolCallPaneClose) -> None:
        fact_index: int | None = None
        if not message.is_error:
            fact_index = self._fact_count
            self._fact_count += 1
        self._tool_call_panes[message.pane_id].finish(
            result=message.result, is_error=message.is_error, fact_index=fact_index
        )

    def on_answer_pane_create(self, message: AnswerPaneCreate) -> None:
        self._fact_count += 1
        pending = self._tool_call_panes.pop(message.pane_id, None)
        if pending is not None:
            pending.remove()
        pane = AnswerPane(pane_id=message.pane_id, content=message.content)
        self.query_one("#conversation", VerticalScroll).mount(pane)

    def on_run_started_message(self, message: RunStartedMessage) -> None:
        self._run_in_flight = True
        self.query_one(StatusLine).counter.reset()
        if self._queued_user_panes:
            self._queued_user_panes[0].queued = False

    def _advance_queue(self) -> None:
        if self._queued_user_panes:
            self._queued_user_panes.pop(0)

    def on_generation_completed_message(self, message: GenerationCompletedMessage) -> None:
        self.query_one(StatusLine).counter.reconcile(message.completion_tokens)

    def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self._run_in_flight = False
        self._stop_status()
        self._advance_queue()

    def on_run_cancelled_message(self, message: RunCancelledMessage) -> None:
        self._run_in_flight = False
        self._stop_status()
        self._advance_queue()

    def action_cancel_run(self) -> None:
        if self._run_in_flight and self._cancel_handle is not None:
            self._cancel_handle.trigger()

    def action_new_session(self) -> None:
        if self._run_in_flight or self._session_handle is None:
            return
        self._session_handle.start_new()
        self._assistant_panes.clear()
        self._thinking_panes.clear()
        self._tool_call_panes.clear()
        self._queued_user_panes.clear()
        self._fact_count = 0
        conversation = self.query_one("#conversation", VerticalScroll)
        status = self.query_one(StatusLine)
        status.stop()
        status.display = False
        splash = self.query_one(Splash)
        splash.session_id = self._session_id()
        for child in list(conversation.children):
            if child is not status and child is not splash:
                child.remove()
        conversation.move_child(status, after=-1)

    def on_error_message(self, message: ErrorMessage) -> None:
        self._run_in_flight = False
        self._stop_status()
        self.query_one("#conversation", VerticalScroll).mount(ErrorPane(message.message))
        self._advance_queue()

    def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
        text = message.text.strip()
        if not text:
            return
        conversation = self.query_one("#conversation", VerticalScroll)
        pane = UserPane(text=message.text)
        if self._queued_user_panes:
            pane.queued = True
        self._queued_user_panes.append(pane)
        conversation.mount(pane)
        status = self.query_one(StatusLine)
        conversation.move_child(status, after=-1)
        status.start()
        self._input_queue.put(text)
