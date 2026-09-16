import time
from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from pico.core.context import estimate_tokens
from pico.tui.theme import PICO_THEME, Theme

SUCCESS_GLYPH = "✓"
ERROR_GLYPH = "✗"
SEPARATOR_GLYPH = "·"
WAITING_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class AssistantPane(Static):
    content_text: reactive[str] = reactive("", repaint=True)
    finished: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, pane_id: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"assistant-{pane_id}")
        self._theme = theme
        self.styles.color = theme.assistant
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def finish(self) -> None:
        self.finished = True

    def render(self) -> Text:
        return Text(self.content_text, style=self._theme.assistant)


class AnswerPane(Static):
    content_text: reactive[str] = reactive("", repaint=True)
    settled: reactive[bool] = reactive(False, repaint=True)
    accepted: reactive[bool] = reactive(False, repaint=True)
    reason: reactive[str | None] = reactive(None, repaint=True)
    verify: reactive[str | None] = reactive(None, repaint=True)

    def __init__(self, pane_id: str, content: str = "", theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"answer-{pane_id}")
        self._theme = theme
        self.set_reactive(AnswerPane.content_text, content)
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def settle(self, content: str, accepted: bool, reason: str | None, verify: str | None) -> None:
        self.content_text = content
        self.accepted = accepted
        self.reason = reason
        self.verify = verify
        self.settled = True

    def render(self) -> Text:
        if not self.settled:
            marker = Text("● ", style=f"bold {self._theme.primary}")
            body = Text(self.content_text, style=f"bold {self._theme.primary}")
            return marker + body
        if self.accepted:
            color = self._theme.success
            marker = Text(f"{SUCCESS_GLYPH} ", style=f"bold {color}")
            body = Text(self.content_text, style=f"bold {color}")
            result = marker + body
            if self.verify is not None:
                result.append(f"\nverified: {self.verify}", style=f"italic {color}")
            return result
        color = self._theme.warning
        marker = Text("↺ ", style=f"bold {color}")
        body = Text(self.content_text, style=f"bold {color}")
        result = marker + body
        if self.reason is not None:
            result.append(f"\nsent back: {self.reason}", style=color)
        return result


class ThinkingPane(Static):
    content_text: reactive[str] = reactive("", repaint=True)
    finished: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, pane_id: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"thinking-{pane_id}")
        self._theme = theme
        self.styles.background = theme.thinking_bg
        self.styles.color = theme.thinking
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def finish(self) -> None:
        self.finished = True

    def render(self) -> Text:
        return Text(self.content_text, style=self._theme.thinking)


class UserPane(Static):
    queued: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, text: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=None)
        self._theme = theme
        self._text = text
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        if self.queued:
            return Text.assemble(
                (self._text, f"bold {self._theme.muted_text}"),
                "  ",
                ("queued", f"italic {self._theme.muted_text}"),
            )
        return Text(self._text, style=f"bold {self._theme.text}")


RESULT_TRUNCATE_LENGTH = 300


def truncate(text: str, limit: int = RESULT_TRUNCATE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… ({len(text) - limit} more chars)"


class ToolCallPane(Static):
    arguments_text: reactive[str] = reactive("", repaint=True)
    result_text: reactive[str] = reactive("", repaint=True)
    finished: reactive[bool] = reactive(False, repaint=True)
    is_error: reactive[bool] = reactive(False, repaint=True)
    fact_index: reactive[int | None] = reactive(None, repaint=True)
    frame_index: reactive[int] = reactive(0, repaint=True)

    def __init__(
        self, pane_id: str, name: str, arguments: str = "", theme: Theme = PICO_THEME
    ) -> None:
        super().__init__(id=f"tool-{pane_id}")
        self._theme = theme
        self.name_label = name
        self.set_reactive(ToolCallPane.arguments_text, arguments)
        self._timer: Timer | None = None
        self.styles.border = ("round", theme.tool_call_border)
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._advance)

    def _advance(self) -> None:
        self.frame_index = (self.frame_index + 1) % len(WAITING_FRAMES)

    def append_arguments_delta(self, text: str) -> None:
        self.arguments_text += text

    def append_result_delta(self, text: str) -> None:
        self.result_text += text

    def finish(
        self,
        result: str,
        is_error: bool,
        fact_index: int | None = None,
        arguments: str | None = None,
    ) -> None:
        if arguments is not None:
            self.arguments_text = arguments
        self.result_text = result
        self.is_error = is_error
        self.fact_index = fact_index
        self.finished = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def render(self) -> Text:
        if self.finished:
            glyph = ERROR_GLYPH if self.is_error else SUCCESS_GLYPH
            color = self._theme.error if self.is_error else self._theme.success
        else:
            glyph = WAITING_FRAMES[self.frame_index]
            color = self._theme.tool_call

        header = Text(f"{glyph} {self.name_label}", style=f"bold {color}")
        if self.fact_index is not None:
            header.append(f"  → fact #{self.fact_index}", style=f"italic {self._theme.muted_text}")

        lines = [header]
        if self.arguments_text:
            lines.append(Text(self.arguments_text, style=self._theme.muted_text))
        if self.result_text:
            result_text = truncate(self.result_text) if self.finished else self.result_text
            lines.append(Text(result_text, style=self._theme.text))

        return Text("\n").join(lines)


class WaitingIndicator(Static):
    frame_index: reactive[int] = reactive(0, repaint=True)
    running: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="waiting-indicator")
        self._theme = theme
        self._timer: Timer | None = None
        self.styles.color = theme.waiting
        self.styles.padding = (0, 1)
        self.display = False

    def start(self) -> None:
        self.frame_index = 0
        self.running = True
        self.display = True
        self._timer = self.set_interval(0.08, self._advance)

    def stop(self) -> None:
        self.running = False
        self.display = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _advance(self) -> None:
        self.frame_index = (self.frame_index + 1) % len(WAITING_FRAMES)

    def render(self) -> Text:
        return Text(WAITING_FRAMES[self.frame_index], style=self._theme.waiting)


ELAPSED_TICK_SECONDS = 1.0


def format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


class ElapsedTimer(Static):
    elapsed: reactive[int] = reactive(0, layout=True)
    running: reactive[bool] = reactive(False)

    def __init__(
        self, theme: Theme = PICO_THEME, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(id="elapsed-timer")
        self._theme = theme
        self._clock = clock
        self._timer: Timer | None = None
        self._started_at = 0.0
        self.styles.color = theme.muted_text

    def start(self) -> None:
        self._started_at = self._clock()
        self.elapsed = 0
        self.running = True
        self._timer = self.set_interval(ELAPSED_TICK_SECONDS, self._tick)

    def stop(self) -> None:
        self.running = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        self.elapsed = int(self._clock() - self._started_at)

    def render(self) -> Text:
        return Text(format_elapsed(self.elapsed), style=self._theme.muted_text)


class TokenCounter(Static):
    tokens: reactive[int] = reactive(0, layout=True)
    reconciled: reactive[bool] = reactive(False, layout=True)

    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="token-counter")
        self._theme = theme
        self._confirmed = 0
        self.styles.color = theme.muted_text

    def reset(self) -> None:
        self._confirmed = 0
        self.tokens = 0
        self.reconciled = False

    def estimate(self, text: str) -> None:
        self.tokens += estimate_tokens(text)
        self.reconciled = False

    def reconcile(self, completion_tokens: int | None) -> None:
        if completion_tokens is None:
            return
        self._confirmed += completion_tokens
        self.tokens = self._confirmed
        self.reconciled = True

    def render(self) -> Text:
        prefix = "" if self.reconciled else "~"
        return Text(f"{prefix}{self.tokens} tokens", style=self._theme.muted_text)


class StatusLine(Horizontal):
    def __init__(self, theme: Theme = PICO_THEME, clock: Callable[[], float] = time.monotonic):
        super().__init__(id="status-line")
        self._clock = clock
        self._theme = theme
        self.display = False

    def compose(self) -> ComposeResult:
        yield WaitingIndicator(self._theme)
        yield ElapsedTimer(self._theme, self._clock)
        yield Static(f" {SEPARATOR_GLYPH} ")
        yield TokenCounter(self._theme)

    @property
    def indicator(self) -> WaitingIndicator:
        return self.query_one(WaitingIndicator)

    @property
    def timer(self) -> ElapsedTimer:
        return self.query_one(ElapsedTimer)

    @property
    def counter(self) -> TokenCounter:
        return self.query_one(TokenCounter)

    def start(self) -> None:
        self.display = True
        self.indicator.start()
        self.timer.start()
        self.counter.reset()

    def stop(self) -> None:
        self.indicator.stop()
        self.timer.stop()


LOGO_TOP = "╭────────╮"
LOGO_PROMPT = "│  "
LOGO_CURSOR = "_"
LOGO_PROMPT_END = "    │"
LOGO_MID = "│        │"
LOGO_BOTTOM = "╰────────╯"
LOGO_LABEL = "PICO"
TAGLINE = "Small model. Real agency."
LOGO_WIDTH = len(LOGO_TOP)
LOGO_INDENT = " " * ((len(TAGLINE) - LOGO_WIDTH) // 2)
LABEL_INDENT = " " * ((len(TAGLINE) - len(LOGO_LABEL)) // 2)


class Splash(Static):
    session_id: reactive[str] = reactive("", repaint=True)

    def __init__(self, session_id: str = "", theme: Theme = PICO_THEME) -> None:
        super().__init__(id="splash")
        self._theme = theme
        self.styles.padding = (1, 0, 1, 2)
        self.set_reactive(Splash.session_id, session_id)

    def render(self) -> Text:
        border = f"bold {self._theme.primary}"
        lines = [
            Text(LOGO_INDENT + LOGO_TOP, style=border),
            Text.assemble(
                (LOGO_INDENT + LOGO_PROMPT, border),
                (">", f"bold {self._theme.success}"),
                (LOGO_CURSOR, f"bold {self._theme.success} blink"),
                (LOGO_PROMPT_END, border),
            ),
            Text(LOGO_INDENT + LOGO_MID, style=border),
            Text(LOGO_INDENT + LOGO_BOTTOM, style=border),
            Text(LABEL_INDENT + LOGO_LABEL, style=f"bold {self._theme.text}"),
            Text(TAGLINE, style=f"italic {self._theme.muted_text}"),
        ]
        if self.session_id:
            lines.append(Text(f"session {self.session_id}", style=self._theme.muted_text))
        return Text("\n").join(lines)


class ErrorPane(Static):
    def __init__(self, message: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="error-pane")
        self._theme = theme
        self._message = message
        self.styles.border = ("heavy", theme.error)
        self.styles.color = theme.error
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        return Text(f"{ERROR_GLYPH} {self._message}", style=f"bold {self._theme.error}")
