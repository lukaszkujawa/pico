from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from pico.tui.theme import PICO_THEME, Theme


class ThinkingBox(Static):
    content_text: reactive[str] = reactive("", repaint=True)
    finished: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, box_id: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"thinking-{box_id}")
        self._theme = theme
        self.styles.border = ("round", theme.thinking)
        self.styles.color = theme.text

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def finish(self) -> None:
        self.finished = True

    def render(self) -> Text:
        style = self._theme.muted_text if self.finished else self._theme.thinking
        return Text(self.content_text, style=style)


class ToolCallBox(Static):
    content_text: reactive[str] = reactive("", repaint=True)
    finished: reactive[bool] = reactive(False, repaint=True)
    is_error: reactive[bool] = reactive(False, repaint=True)

    def __init__(self, box_id: str, name: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"tool-{box_id}")
        self._theme = theme
        self.name_label = name
        self.styles.border = ("round", theme.tool_call)
        self.styles.color = theme.text

    def append_delta(self, text: str) -> None:
        self.content_text += text

    def finish(self, is_error: bool) -> None:
        self.is_error = is_error
        self.finished = True

    def render(self) -> Text:
        if self.finished:
            style = self._theme.error if self.is_error else self._theme.success
        else:
            style = self._theme.tool_call
        return Text(f"{self.name_label}\n{self.content_text}", style=style)
