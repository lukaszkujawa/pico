import time
from collections.abc import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from pico.core.context import estimate_tokens
from pico.tui.commands import Completion, Row
from pico.tui.theme import PICO_THEME, Theme

SUCCESS_GLYPH = "✓"
ERROR_GLYPH = "✗"
SEPARATOR_GLYPH = "·"
PENDING_GLYPH = "●"
RETRY_GLYPH = "↺"
INCOMPLETE_GLYPH = "…"
WAITING_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class AssistantPane(Static):
    content_text: reactive[str] = reactive("", layout=True)

    def __init__(self, pane_id: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"assistant-{pane_id}")
        self._theme = theme
        self.styles.color = theme.assistant
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def render(self) -> Text:
        return Text(self.content_text, style=self._theme.assistant)


class AnswerPane(Static):
    content_text: reactive[str] = reactive("", layout=True)
    settled: reactive[bool] = reactive(False, repaint=True)
    accepted: reactive[bool] = reactive(False, repaint=True)
    reason: reactive[str | None] = reactive(None, repaint=True)
    verify: reactive[str | None] = reactive(None, repaint=True)
    complete: reactive[bool] = reactive(True, repaint=True)

    def __init__(self, pane_id: str, content: str = "", theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"answer-{pane_id}")
        self._theme = theme
        self.set_reactive(AnswerPane.content_text, content)
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def settle(
        self,
        content: str,
        accepted: bool,
        reason: str | None,
        verify: str | None,
        complete: bool = True,
    ) -> None:
        self.content_text = content
        self.accepted = accepted
        self.reason = reason
        self.verify = verify
        self.complete = complete
        self.settled = True

    def render(self) -> Text:
        body_style = f"bold {self._theme.text}"
        if not self.settled:
            marker = Text(f"{PENDING_GLYPH} ", style=f"bold {self._theme.accent}")
            return marker + Text(self.content_text, style=body_style)
        if self.accepted:
            glyph = SUCCESS_GLYPH if self.complete else INCOMPLETE_GLYPH
            tone = self._theme.success if self.complete else self._theme.muted_text
            marker = Text(f"{glyph} ", style=f"bold {tone}")
            result = marker + Text(self.content_text, style=body_style)
            if self.verify is not None:
                result.append(
                    f"\nverified: {self.verify}", style=f"italic {self._theme.muted_text}"
                )
            return result
        marker = Text(f"{RETRY_GLYPH} ", style=f"bold {self._theme.warning}")
        result = marker + Text(self.content_text, style=body_style)
        if self.reason is not None:
            result.append(f"\nsent back: {self.reason}", style=self._theme.muted_text)
        return result


class ThinkingPane(Static):
    content_text: reactive[str] = reactive("", layout=True)

    def __init__(self, pane_id: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"thinking-{pane_id}")
        self._theme = theme
        self.styles.background = theme.thinking_bg
        self.styles.color = theme.thinking
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def render(self) -> Text:
        return Text(self.content_text, style=self._theme.thinking)


class UserPane(Static):
    queued: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, text: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=None)
        self._theme = theme
        self._text = text
        self.styles.color = theme.user
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        if self.queued:
            return Text.assemble(
                (self._text, f"bold {self._theme.muted_text}"),
                "  ",
                ("queued", f"italic {self._theme.muted_text}"),
            )
        return Text(self._text, style=f"bold {self._theme.user}")


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
            glyph_color = self._theme.error if self.is_error else self._theme.success
        else:
            glyph = WAITING_FRAMES[self.frame_index]
            glyph_color = self._theme.waiting

        header = Text(f"{glyph} ", style=f"bold {glyph_color}")
        header.append(self.name_label, style=f"bold {self._theme.tool_call}")
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

    def start(self) -> None:
        self.stop()
        self.frame_index = 0
        self.running = True
        self._timer = self.set_interval(0.08, self._advance)

    def stop(self) -> None:
        self.running = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _advance(self) -> None:
        self.frame_index = (self.frame_index + 1) % len(WAITING_FRAMES)

    def render(self) -> Text:
        if not self.running:
            return Text(" ")
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
        self.stop()
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


METER_WIDTH = 10
METER_FILLED = "█"
METER_EMPTY = "░"
OVER_BUDGET_RATIO = 0.9


def format_thousands(value: int) -> str:
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k".replace(".0k", "k")


class ContextMeter(Static):
    used: reactive[int] = reactive(0, layout=True)
    pressure: reactive[bool] = reactive(False, repaint=True)
    dying: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, context_size: int, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="context-meter")
        self._theme = theme
        self._context_size = context_size

    def reset(self) -> None:
        self.used = 0
        self.pressure = False
        self.dying = False

    @property
    def ratio(self) -> float:
        if self._context_size <= 0:
            return 0.0
        return min(self.used / self._context_size, 1.0)

    def _band(self) -> str:
        if self.dying or self.ratio >= OVER_BUDGET_RATIO:
            return self._theme.error
        if self.pressure:
            return self._theme.meter_warning
        return self._theme.meter

    def render(self) -> Text:
        ratio = self.ratio
        filled = round(ratio * METER_WIDTH)
        bar = Text(METER_FILLED * filled, style=self._band())
        bar.append(METER_EMPTY * (METER_WIDTH - filled), style=self._theme.meter_empty)
        numbers = f" {format_thousands(self.used)}/{format_thousands(self._context_size)}"
        bar.append(numbers, style=self._theme.muted_text)
        return bar


class RequestCounter(Static):
    requests: reactive[int] = reactive(0, layout=True)

    def __init__(self, theme: Theme = PICO_THEME, budget: int | None = None) -> None:
        super().__init__(id="request-counter")
        self._theme = theme
        self._budget = budget

    def reset(self) -> None:
        self.requests = 0

    def increment(self) -> None:
        self.requests += 1

    def render(self) -> Text:
        if self._budget is None:
            return Text(f"{self.requests} req", style=self._theme.muted_text)
        return Text(f"{self.requests}/{self._budget} req", style=self._theme.muted_text)


class ActivityStrip(Horizontal):
    def __init__(
        self, theme: Theme = PICO_THEME, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(id="activity-strip")
        self._theme = theme
        self._clock = clock
        self.display = False

    def compose(self) -> ComposeResult:
        yield WaitingIndicator(self._theme)
        yield Static(" ", classes="stats-separator")
        yield TokenCounter(self._theme)
        yield Static(f" {SEPARATOR_GLYPH} ", classes="stats-separator")
        yield ElapsedTimer(self._theme, self._clock)

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

    def reset(self) -> None:
        self.stop()
        self.timer.elapsed = 0
        self.counter.reset()
        self.display = False


class StatsStrip(Horizontal):
    def __init__(
        self,
        context_size: int,
        max_steps: int | None = None,
        theme: Theme = PICO_THEME,
    ):
        super().__init__(id="stats-strip")
        self._context_size = context_size
        self._max_steps = max_steps
        self._theme = theme

    def compose(self) -> ComposeResult:
        yield Static("ctx", classes="stats-label")
        yield ContextMeter(self._context_size, self._theme)
        yield Static(f" {SEPARATOR_GLYPH} ", classes="stats-separator")
        yield RequestCounter(self._theme, self._max_steps)

    @property
    def meter(self) -> ContextMeter:
        return self.query_one(ContextMeter)

    @property
    def requests(self) -> RequestCounter:
        return self.query_one(RequestCounter)

    def reset(self) -> None:
        self.meter.reset()
        self.requests.reset()


LOGO_TOP = "╭────────╮"
LOGO_PROMPT = "│  "
LOGO_CURSOR = "_"
LOGO_PROMPT_END = "    │"
LOGO_MID = "│        │"
LOGO_BOTTOM = "╰────────╯"
LOGO_LABEL = "PICO"
TAGLINE = "Small model. Real agency."
LOGO_HEIGHT = 4
COLUMN_GAP = " " * 2


class Splash(Static):
    session_id: reactive[str] = reactive("", repaint=True)

    def __init__(
        self, session_id: str = "", local_directory: str = "", theme: Theme = PICO_THEME
    ) -> None:
        super().__init__(id="splash")
        self._theme = theme
        self._local_directory = local_directory
        self.styles.padding = (1, 0, 1, 2)
        self.set_reactive(Splash.session_id, session_id)

    def render(self) -> Text:
        border = f"bold {self._theme.tool_call_border}"
        art_lines = [
            Text(LOGO_TOP, style=border),
            Text.assemble(
                (LOGO_PROMPT, border),
                (">", f"bold {self._theme.success}"),
                (LOGO_CURSOR, f"bold {self._theme.success} blink"),
                (LOGO_PROMPT_END, border),
            ),
            Text(LOGO_MID, style=border),
            Text(LOGO_BOTTOM, style=border),
        ]

        right_lines = [
            Text(LOGO_LABEL, style=f"bold {self._theme.text}"),
            Text(TAGLINE, style=self._theme.muted_text),
        ]
        if self.session_id:
            right_lines.append(Text(f"session {self.session_id}", style=self._theme.muted_text))
        right_lines.append(Text(self._local_directory, style=self._theme.muted_text))

        pad_top = (LOGO_HEIGHT - len(right_lines)) // 2
        right_column = (
            [Text("")] * pad_top
            + right_lines
            + [Text("")] * (LOGO_HEIGHT - len(right_lines) - pad_top)
        )

        lines = [
            art_line + Text(COLUMN_GAP) + right_line
            for art_line, right_line in zip(art_lines, right_column, strict=True)
        ]
        return Text("\n").join(lines)


SELECTED_GLYPH = "▸"
CURRENT_GLYPH = "•"


class CommandMenu(Static):
    completion: reactive[Completion | None] = reactive(None, layout=True)
    selected: reactive[int] = reactive(0, repaint=True)

    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="command-menu")
        self._theme = theme
        self.styles.padding = (0, 1)
        self.display = False

    @property
    def rows(self) -> tuple[Row, ...]:
        return () if self.completion is None else self.completion.rows

    def show(self, completion: Completion) -> None:
        previous = self.selection
        self.completion = completion
        self.selected = next(
            (index for index, row in enumerate(completion.rows) if row.label == previous), 0
        )
        self.display = bool(completion.rows) or completion.error is not None

    def hide(self) -> None:
        self.completion = None
        self.selected = 0
        self.display = False

    @property
    def selection(self) -> str | None:
        rows = self.rows
        return rows[self.selected].label if rows else None

    def move(self, offset: int) -> None:
        if self.rows:
            self.selected = (self.selected + offset) % len(self.rows)

    def accept(self) -> str | None:
        if self.completion is None or not self.rows:
            return None
        return self.completion.accepted(self.rows[self.selected])

    def render(self) -> Text:
        if self.completion is None:
            return Text("")
        if self.completion.error is not None:
            return Text(f"{ERROR_GLYPH} {self.completion.error}", style=f"bold {self._theme.error}")
        lines: list[Text] = []
        width = max(len(self._label(row)) for row in self.rows)
        for index, row in enumerate(self.rows):
            chosen = index == self.selected
            line = Text(
                f"{SELECTED_GLYPH} " if chosen else "  ",
                style=f"bold {self._theme.accent}",
            )
            line.append(
                row.label, style=f"bold {self._theme.text if chosen else self._theme.tool_call}"
            )
            if row.marked:
                line.append(f" {CURRENT_GLYPH}", style=self._theme.success)
            line.append(" " * (width - len(self._label(row))))
            if row.hint:
                line.append(f"  {row.hint}", style=self._theme.muted_text)
            if chosen:
                line.stylize(f"on {self._theme.selection_bg}")
            lines.append(line)
        return Text("\n").join(lines)

    def _label(self, row: Row) -> str:
        return f"{row.label} {CURRENT_GLYPH}" if row.marked else row.label


class SystemPane(Static):
    def __init__(self, message: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=None)
        self._theme = theme
        self._message = message
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        return Text(self._message, style=f"italic {self._theme.muted_text}")


class ErrorPane(Static):
    def __init__(self, message: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="error-pane")
        self._theme = theme
        self._message = message
        self.styles.border = ("heavy", theme.tool_call_border)
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        glyph = Text(f"{ERROR_GLYPH} ", style=f"bold {self._theme.error}")
        return glyph + Text(self._message, style=f"bold {self._theme.text}")
