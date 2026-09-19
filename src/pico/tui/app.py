import json
import queue
import threading
from pathlib import Path
from typing import ClassVar, Protocol, cast

from textual import events
from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Rule, Static, TextArea
from textual.worker import Worker, get_current_worker  # pyright: ignore[reportUnknownVariableType]

from pico.core.bus import Bus
from pico.tui import commands
from pico.tui.commands import Options
from pico.tui.messages import (
    AnswerPaneCreate,
    AnswerPaneSettle,
    AssistantPaneCreate,
    AssistantPaneDelta,
    CommandAccepted,
    CommandCompleted,
    CommandMenuKey,
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
    ActivityStrip,
    AnswerPane,
    AssistantPane,
    CommandMenu,
    ErrorPane,
    Splash,
    StatsStrip,
    SystemPane,
    ThinkingPane,
    ToolCallPane,
    UserPane,
)


class CancelHandle(Protocol):
    def trigger(self) -> None: ...


class ModelSwitch(Protocol):
    @property
    def current(self) -> str: ...

    def available(self) -> Options: ...

    def switch_to(self, model: str) -> None: ...


class SessionHandle(Protocol):
    @property
    def session_id(self) -> str: ...

    def start_new(self) -> None: ...


NEWLINE_KEYS = {"ctrl+j", "shift+enter"}
DEFAULT_CONTEXT_SIZE = 8192


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


MENU_KEYS = {"up", "down", "escape"}


class ChatInput(TextArea):
    async def _on_key(self, event: events.Key) -> None:
        menu = self.screen.query_one(CommandMenu)
        if menu.display and event.key in MENU_KEYS:
            event.stop()
            event.prevent_default()
            self.post_message(CommandMenuKey(key=event.key))
            return
        if event.key == "tab" and menu.display:
            event.stop()
            event.prevent_default()
            completed = self._accept(menu)
            if completed is not None:
                self.post_message(CommandCompleted(text=completed))
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if menu.display:
                accepted = self._accept(menu)
                if accepted is not None:
                    self.post_message(CommandAccepted(text=accepted))
                    return
            text = self.text
            self.clear()
            self.post_message(UserInputSubmitted(text=text, typed=True))
            return
        if event.key in NEWLINE_KEYS:
            event.key = "enter"
        await super()._on_key(event)

    def _accept(self, menu: CommandMenu) -> str | None:
        app = cast("PicoApp", self.app)  # pyright: ignore[reportUnknownMemberType]
        completion = app.command_completion(self.text)
        if completion is None or not completion.rows:
            return None
        rows = completion.rows
        row = next((row for row in rows if row.label == menu.selection), rows[0])
        return completion.accepted(row)


class Conversation(VerticalScroll):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.pinned = True
        self._self_scrolling = False

    def scroll_end(self, *args: object, **kwargs: object) -> None:
        self._self_scrolling = True
        try:
            super().scroll_end(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            self._self_scrolling = False
        self.pinned = True

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if not self._self_scrolling:
            self.pinned = round(new_value) >= self.max_scroll_y


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
    CSS_PATH = Path(__file__).parent / "app.tcss"
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
        initial_prompt: str | None = None,
        context_size: int = DEFAULT_CONTEXT_SIZE,
        max_steps: int | None = None,
        model_switch: ModelSwitch | None = None,
    ) -> None:
        super().__init__()
        self._bus = bus
        self._context_size = context_size
        self._max_steps = max_steps
        self._input_queue = input_queue
        self._cancel_handle = cancel_handle
        self._session_handle = session_handle
        self._model_switch = model_switch
        self._initial_prompt = initial_prompt
        self._run_in_flight = False
        self._error_shown_this_run = False
        self._assistant_panes: dict[str, AssistantPane] = {}
        self._thinking_panes: dict[str, ThinkingPane] = {}
        self._tool_call_panes: dict[str, ToolCallPane] = {}
        self._answer_panes: dict[str, AnswerPane] = {}
        self._answer_arguments: dict[str, str] = {}
        self._queued_user_panes: list[UserPane] = []
        self._pending_token_text: str = ""
        self._flush_timer: Timer | None = None
        self._menu_models: Options | None = None
        self._completed_text: str | None = None
        self._models_worker: Worker[None] | None = None

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
        with Conversation(id="conversation"):
            yield Splash(self._session_id(), self._local_directory())
            yield ActivityStrip()
        yield Rule()
        with Vertical(id="footer"):
            yield CommandMenu()
            yield InputBar()
            yield Rule()
            yield StatsStrip(self._context_size, self._max_steps)

    def on_mount(self) -> None:
        self.register_theme(PICO_THEME.to_textual())
        self.theme = PICO_THEME.name
        self.query_one("#user-input", ChatInput).focus()
        self._conversation().anchor(False)
        threading.Thread(target=self._consume_bus, daemon=True).start()
        self._flush_timer = self.set_interval(0.05, self._flush_pending_updates)
        if self._initial_prompt is not None:
            self.post_message(UserInputSubmitted(text=self._initial_prompt))

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

    def _stats(self) -> StatsStrip:
        return self.query_one(StatsStrip)

    def _activity(self) -> ActivityStrip:
        return self.query_one(ActivityStrip)

    def _stop_status(self) -> None:
        self._activity().stop()

    def _conversation(self) -> Conversation:
        return self.query_one("#conversation", Conversation)

    async def _mount_at_bottom(self, pane: Static) -> None:
        conversation = self._conversation()
        pinned = conversation.pinned
        await conversation.mount(pane, before=self._activity())
        if pinned:
            conversation.scroll_end(animate=False)

    def _queue_token_estimate(self, text: str) -> None:
        self._pending_token_text += text

    def _flush_pending_updates(self) -> None:
        try:
            conversation = self._conversation()
            if self._pending_token_text:
                self._activity().counter.estimate(self._pending_token_text)
                self._pending_token_text = ""
            if conversation.pinned:
                conversation.scroll_end(animate=False, immediate=True)
        except NoMatches:
            return

    def on_unmount(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.stop()

    async def on_assistant_pane_create(self, message: AssistantPaneCreate) -> None:
        pane = AssistantPane(pane_id=message.pane_id)
        self._assistant_panes[message.pane_id] = pane
        await self._mount_at_bottom(pane)

    def on_assistant_pane_delta(self, message: AssistantPaneDelta) -> None:
        self._assistant_panes[message.pane_id].append_delta(message.text)
        self._queue_token_estimate(message.text)

    async def on_thinking_pane_create(self, message: ThinkingPaneCreate) -> None:
        pane = ThinkingPane(pane_id=message.pane_id)
        self._thinking_panes[message.pane_id] = pane
        await self._mount_at_bottom(pane)

    def on_thinking_pane_delta(self, message: ThinkingPaneDelta) -> None:
        self._thinking_panes[message.pane_id].append_delta(message.text)
        self._queue_token_estimate(message.text)

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
            return
        pane = self._tool_call_panes.get(message.pane_id)
        if pane is None:
            pane = ToolCallPane(pane_id=message.pane_id, name=message.name)
            self._tool_call_panes[message.pane_id] = pane
            await self._mount_at_bottom(pane)
        pane.append_arguments_delta(message.text)

    def on_tool_call_pane_result_delta(self, message: ToolCallPaneResultDelta) -> None:
        pane = self._tool_call_panes.get(message.pane_id)
        if pane is not None:
            pane.append_result_delta(message.text)

    def on_tool_call_pane_close(self, message: ToolCallPaneClose) -> None:
        pane = self._tool_call_panes.get(message.pane_id)
        if pane is None:
            return
        pane.finish(
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

    async def on_answer_pane_settle(self, message: AnswerPaneSettle) -> None:
        self._answer_arguments.pop(message.pane_id, None)
        pane = self._answer_panes.get(message.pane_id)
        if pane is None:
            pane = AnswerPane(pane_id=message.pane_id)
            self._answer_panes[message.pane_id] = pane
            await self._mount_at_bottom(pane)
        pane.settle(
            content=message.content,
            accepted=message.accepted,
            reason=message.reason,
            verify=message.verify,
            complete=message.complete,
        )

    def on_run_started_message(self, message: RunStartedMessage) -> None:
        self._run_in_flight = True
        self._error_shown_this_run = False
        self._activity().start()
        if self._queued_user_panes:
            self._queued_user_panes[0].queued = False

    def _advance_queue(self) -> None:
        if self._queued_user_panes:
            self._queued_user_panes.pop(0)

    def on_generation_completed_message(self, message: GenerationCompletedMessage) -> None:
        stats = self._stats()
        if message.iteration is None:
            stats.requests.increment()
        else:
            stats.requests.requests = message.iteration
        if message.prompt_tokens is not None:
            stats.meter.used = message.prompt_tokens
        stats.meter.pressure = message.pressure
        stats.meter.dying = (
            self._max_steps is not None
            and message.iteration is not None
            and message.iteration > self._max_steps
        )
        counter = self._activity().counter
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
            self._stop_status()
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
        conversation = self._conversation()
        self._stats().reset()
        activity = self._activity()
        activity.reset()
        splash = self.query_one(Splash)
        splash.session_id = self._session_id()
        for child in list(conversation.children):
            if child is not splash and child is not activity:
                child.remove()
        conversation.pinned = True

    async def on_error_message(self, message: ErrorMessage) -> None:
        self._error_shown_this_run = True
        self._stop_status()
        await self._mount_at_bottom(ErrorPane(message.message))

    def _menu(self) -> CommandMenu:
        return self.query_one(CommandMenu)

    def command_completion(self, text: str) -> commands.Completion | None:
        switch = self._model_switch
        current = None if switch is None else switch.current
        completion = commands.complete(text, self._menu_models, current)
        if completion is not None and completion.pending and switch is None:
            return None
        return completion

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.text == self._completed_text:
            return
        self._completed_text = None
        self._refresh_menu(event.text_area.text)

    def _refresh_menu(self, text: str) -> None:
        completion = self.command_completion(text)
        if completion is None:
            self._hide_menu()
            return
        if completion.pending and self._models_worker is None and self._model_switch is not None:
            self._models_worker = self._fetch_models(self._model_switch)
        self._menu().show(completion)

    def _hide_menu(self) -> None:
        self._menu_models = None
        if self._models_worker is not None:
            self._models_worker.cancel()
            self._models_worker = None
        self._menu().hide()

    def _fetch_models(self, switch: ModelSwitch) -> Worker[None]:
        def fetch() -> None:
            options = switch.available()
            worker = cast(Worker[None], get_current_worker())
            if not worker.is_cancelled:
                self.call_from_thread(self._apply_models, worker, options)

        return self.run_worker(fetch, thread=True, exclusive=True, group="model-fetch")

    def _apply_models(self, worker: Worker[None], options: Options) -> None:
        if worker is not self._models_worker:
            return
        self._models_worker = None
        self._menu_models = options
        self._refresh_menu(self.query_one("#user-input", ChatInput).text)

    def on_command_menu_key(self, message: CommandMenuKey) -> None:
        menu = self._menu()
        match message.key:
            case "up":
                menu.move(-1)
            case "down":
                menu.move(1)
            case _:
                self._hide_menu()

    async def on_command_accepted(self, message: CommandAccepted) -> None:
        text_input = self.query_one("#user-input", ChatInput)
        if commands.awaits_argument(message.text):
            self._fill_input(text_input, f"{message.text} ")
            return
        self._hide_menu()
        text_input.clear()
        await self.run_command(message.text)

    def on_command_completed(self, message: CommandCompleted) -> None:
        text_input = self.query_one("#user-input", ChatInput)
        if commands.awaits_argument(message.text):
            self._fill_input(text_input, f"{message.text} ")
            return
        self._completed_text = message.text
        self._fill_input(text_input, message.text)
        self._hide_menu()

    def _fill_input(self, text_input: "ChatInput", text: str) -> None:
        text_input.text = text
        text_input.move_cursor(text_input.document.end)

    async def _say(self, message: str) -> None:
        await self._mount_at_bottom(SystemPane(message))

    async def run_command(self, text: str) -> None:
        name, _, argument = text.removeprefix("/").partition(" ")
        match name:
            case "quit":
                self.exit()
            case "model":
                await self._switch_model(argument.strip())
            case _:
                await self._say(f"unknown command /{name} — known: {commands.KNOWN_COMMANDS}")

    async def _switch_model(self, model: str) -> None:
        if self._model_switch is None:
            return
        if not model:
            await self._say(f"model → {self._model_switch.current}")
            return
        self._model_switch.switch_to(model)
        await self._say(f"model → {model}")

    async def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
        text = message.text.strip()
        if not text:
            return
        if message.typed and text.startswith("/"):
            await self.run_command(text)
            return
        pane = UserPane(text=message.text)
        if self._queued_user_panes:
            pane.queued = True
        self._queued_user_panes.append(pane)
        await self._mount_at_bottom(pane)
        self._activity().start()
        self._input_queue.put(text)
