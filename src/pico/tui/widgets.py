from rich.text import Text
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from pico.tui.theme import PICO_THEME, Theme

SUCCESS_GLYPH = "✓"
ERROR_GLYPH = "✗"
PENDING_GLYPH = "…"
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
    def __init__(self, text: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=None)
        self._theme = theme
        self._text = text
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        return Text(self._text, style=f"bold {self._theme.text}")


class ToolCallPane(Static):
    content_text: reactive[str] = reactive("", repaint=True)
    finished: reactive[bool] = reactive(False, repaint=True)
    is_error: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, pane_id: str, name: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"tool-{pane_id}")
        self._theme = theme
        self.name_label = name
        self.styles.border = ("round", theme.tool_call_border)
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def finish(self, is_error: bool) -> None:
        self.is_error = is_error
        self.finished = True

    def render(self) -> Text:
        if self.finished:
            glyph = ERROR_GLYPH if self.is_error else SUCCESS_GLYPH
            color = self._theme.error if self.is_error else self._theme.success
        else:
            glyph = PENDING_GLYPH
            color = self._theme.tool_call

        header = Text(f"{glyph} {self.name_label}", style=f"bold {color}")
        body = Text(self.content_text, style=self._theme.muted_text)
        return Text("\n").join([header, body]) if self.content_text else header


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
