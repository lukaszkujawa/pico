import json
import queue
import threading
from pathlib import Path
from typing import ClassVar, Protocol, cast

from textual import events
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Rule, Static, TextArea

from pico.core.bus import Bus
from pico.tui.messages import (
    AnswerPaneCreate,
    AnswerPaneSettle,
    AssistantPaneCreate,
    AssistantPaneDelta,
    ErrorMessage,
    GenerationCompletedMessage,
    RunCancelledMessage,
    RunFinishedMessage,
    RunStartedMessage,
    ThinkingPaneCreate,
    ThinkingPaneDelta,
    ToolCallPaneArgumentsDelta,
    ToolCallPaneClose,
    ToolCallPaneCreate,
    ToolCallPaneResultDelta,
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


def extract_answer_content(arguments_text: str) -> str | None:
    try:
        parsed: object = json.loads(arguments_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    fields = cast(dict[str, object], parsed)
    content = fields.get("content")
    return content if isinstance(content, str) else None


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
        scrollbar-size-vertical: 0;
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
        self._error_shown_this_run = False
        self._assistant_panes: dict[str, AssistantPane] = {}
        self._thinking_panes: dict[str, ThinkingPane] = {}
        self._tool_call_panes: dict[str, ToolCallPane] = {}
        self._answer_panes: dict[str, AnswerPane] = {}
        self._answer_arguments: dict[str, str] = {}
        self._queued_user_panes: list[UserPane] = []
        self._pending_token_text: str = ""
        self._scroll_dirty: bool = False
        self._flush_timer: Timer | None = None

    def _session_id(self) -> str:
        return "" if self._session_handle is None else self._session_handle.session_id

    def _local_directory(self) -> str:
        cwd = Path.cwd()
        home = Path.home()
        try:
            return f"~/{cwd.relative_to(home)}" if cwd != home else "~"
        except ValueError:
            return str(cwd)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="conversation"):
            yield Splash(self._session_id(), self._local_directory())
            yield StatusLine()
        yield Rule()
        with Vertical(id="footer"):
            yield InputBar()
            yield Rule()

    def on_mount(self) -> None:
        self.register_theme(PICO_THEME.to_textual())
        self.theme = PICO_THEME.name
        self.query_one("#user-input", ChatInput).focus()
        conversation = self.query_one("#conversation", VerticalScroll)
        conversation.anchor(False)
        threading.Thread(target=self._consume_bus, daemon=True).start()
        self._flush_timer = self.set_interval(0.05, self._flush_pending_updates)

    def on_click(self, event: events.Click) -> None:
        text_input = self.query_one("#user-input", ChatInput)
        if event.widget is not text_input:
            text_input.focus()

    def _consume_bus(self) -> None:
        for event in self._bus.subscribe():
            try:
                message = translate(event)
                if message is not None:
                    self.post_message(message)
            except Exception as error:
                self.post_message(ErrorMessage(message=f"UI event handling failed: {error}"))

    def _stop_status(self) -> None:
        self.query_one(StatusLine).stop()

    def _conversation(self) -> VerticalScroll:
        return self.query_one("#conversation", VerticalScroll)

    async def _mount_at_bottom(self, pane: Static) -> None:
        conversation = self._conversation()
        at_bottom = conversation.scroll_offset.y >= conversation.max_scroll_y
        await conversation.mount(pane, before=self.query_one(StatusLine))
        if at_bottom:
            conversation.scroll_end(animate=False)

    def _stick_to_bottom(self) -> None:
        self._scroll_dirty = True

    def _queue_token_estimate(self, text: str) -> None:
        self._pending_token_text += text

    def _flush_pending_updates(self) -> None:
        if self._pending_token_text:
            self.query_one(StatusLine).counter.estimate(self._pending_token_text)
            self._pending_token_text = ""
        if self._scroll_dirty:
            self._scroll_dirty = False
            conversation = self._conversation()
            if conversation.scroll_offset.y >= conversation.max_scroll_y:
                conversation.scroll_end(animate=False)

    async def on_assistant_pane_create(self, message: AssistantPaneCreate) -> None:
        pane = AssistantPane(pane_id=message.pane_id)
        self._assistant_panes[message.pane_id] = pane
        await self._mount_at_bottom(pane)

    def on_assistant_pane_delta(self, message: AssistantPaneDelta) -> None:
        self._assistant_panes[message.pane_id].append_delta(message.text)
        self._queue_token_estimate(message.text)
        self._stick_to_bottom()

    async def on_thinking_pane_create(self, message: ThinkingPaneCreate) -> None:
        pane = ThinkingPane(pane_id=message.pane_id)
        self._thinking_panes[message.pane_id] = pane
        await self._mount_at_bottom(pane)

    def on_thinking_pane_delta(self, message: ThinkingPaneDelta) -> None:
        self._thinking_panes[message.pane_id].append_delta(message.text)
        self._queue_token_estimate(message.text)
        self._stick_to_bottom()

    async def on_tool_call_pane_create(self, message: ToolCallPaneCreate) -> None:
        if message.pane_id in self._tool_call_panes:
            return
        pane = ToolCallPane(pane_id=message.pane_id, name=message.name, arguments=message.arguments)
        self._tool_call_panes[message.pane_id] = pane
        await self._mount_at_bottom(pane)

    async def on_tool_call_pane_arguments_delta(self, message: ToolCallPaneArgumentsDelta) -> None:
        if message.name == "answer":
            answer_pane = self._answer_panes.get(message.pane_id)
            if answer_pane is None:
                answer_pane = AnswerPane(pane_id=message.pane_id)
                self._answer_panes[message.pane_id] = answer_pane
                await self._mount_at_bottom(answer_pane)
            raw = self._answer_arguments.get(message.pane_id, "") + message.text
            self._answer_arguments[message.pane_id] = raw
            content = extract_answer_content(raw)
            if content is not None:
                answer_pane.content_text = content
            self._stick_to_bottom()
            return
        pane = self._tool_call_panes.get(message.pane_id)
        if pane is None:
            pane = ToolCallPane(pane_id=message.pane_id, name=message.name)
            self._tool_call_panes[message.pane_id] = pane
            await self._mount_at_bottom(pane)
        pane.append_arguments_delta(message.text)
        self._stick_to_bottom()

    def on_tool_call_pane_result_delta(self, message: ToolCallPaneResultDelta) -> None:
        self._tool_call_panes[message.pane_id].append_result_delta(message.text)
        self._stick_to_bottom()

    def on_tool_call_pane_close(self, message: ToolCallPaneClose) -> None:
        self._tool_call_panes[message.pane_id].finish(
            result=message.result,
            is_error=message.is_error,
            fact_index=message.fact_id,
            arguments=message.arguments,
        )

    async def on_answer_pane_create(self, message: AnswerPaneCreate) -> None:
        if message.pane_id in self._answer_panes:
            return
        pane = AnswerPane(pane_id=message.pane_id)
        self._answer_panes[message.pane_id] = pane
        await self._mount_at_bottom(pane)

    def on_answer_pane_settle(self, message: AnswerPaneSettle) -> None:
        self._answer_arguments.pop(message.pane_id, None)
        self._answer_panes[message.pane_id].settle(
            content=message.content,
            accepted=message.accepted,
            reason=message.reason,
            verify=message.verify,
        )

    def on_run_started_message(self, message: RunStartedMessage) -> None:
        self._run_in_flight = True
        self._error_shown_this_run = False
        self.query_one(StatusLine).start()
        if self._queued_user_panes:
            self._queued_user_panes[0].queued = False

    def _advance_queue(self) -> None:
        if self._queued_user_panes:
            self._queued_user_panes.pop(0)

    def on_generation_completed_message(self, message: GenerationCompletedMessage) -> None:
        counter = self.query_one(StatusLine).counter
        if message.completion_tokens is None:
            if self._pending_token_text:
                counter.estimate(self._pending_token_text)
                self._pending_token_text = ""
            return
        self._pending_token_text = ""
        counter.reconcile(message.completion_tokens)

    async def on_run_finished_message(self, message: RunFinishedMessage) -> None:
        self._run_in_flight = False
        self._stop_status()
        if message.error is not None and not self._error_shown_this_run:
            await self._mount_at_bottom(ErrorPane(message.error))
        self._advance_queue()

    def on_run_cancelled_message(self, message: RunCancelledMessage) -> None:
        self._run_in_flight = False
        self._stop_status()
        self._close_open_tool_call_panes()
        self._advance_queue()

    def _close_open_tool_call_panes(self) -> None:
        for pane in self._tool_call_panes.values():
            if not pane.finished:
                pane.finish(result="", is_error=True)

    def action_cancel_run(self) -> None:
        if self._run_in_flight and self._cancel_handle is not None:
            self._cancel_handle.trigger()

    def action_new_session(self) -> None:
        if self._run_in_flight or self._session_handle is None:
            return
        self._close_open_tool_call_panes()
        self._session_handle.start_new()
        self._assistant_panes.clear()
        self._thinking_panes.clear()
        self._tool_call_panes.clear()
        self._answer_panes.clear()
        self._answer_arguments.clear()
        self._queued_user_panes.clear()
        conversation = self.query_one("#conversation", VerticalScroll)
        status = self.query_one(StatusLine)
        status.stop()
        status.display = False
        splash = self.query_one(Splash)
        splash.session_id = self._session_id()
        for child in list(conversation.children):
            if child is not splash and child is not status:
                child.remove()

    async def on_error_message(self, message: ErrorMessage) -> None:
        self._run_in_flight = False
        self._error_shown_this_run = True
        self._stop_status()
        await self._mount_at_bottom(ErrorPane(message.message))
        self._advance_queue()

    async def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
        text = message.text.strip()
        if not text:
            return
        pane = UserPane(text=message.text)
        if self._queued_user_panes:
            pane.queued = True
        self._queued_user_panes.append(pane)
        await self._mount_at_bottom(pane)
        self.query_one(StatusLine).start()
        self._input_queue.put(text)
