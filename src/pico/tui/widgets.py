from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from pico.tui.theme import PICO_THEME, Theme

SUCCESS_GLYPH = "✓"
ERROR_GLYPH = "✗"
PENDING_GLYPH = "…"


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
