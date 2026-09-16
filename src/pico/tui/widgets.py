from rich.text import Text
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from pico.tui.theme import PICO_THEME, Theme

SUCCESS_GLYPH = "✓"
ERROR_GLYPH = "✗"
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
    def __init__(self, pane_id: str, content: str, theme: Theme = PICO_THEME) -> None:
        super().__init__(id=f"answer-{pane_id}")
        self._theme = theme
        self._content = content
        self.styles.color = theme.primary
        self.styles.padding = (0, 1)

    def render(self) -> Text:
        marker = Text("● ", style=f"bold {self._theme.primary}")
        body = Text(self._content, style=f"bold {self._theme.primary}")
        return marker + body


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
        self.arguments_text = arguments
        self._timer: Timer | None = None
        self.styles.border = ("round", theme.tool_call_border)
        self.styles.color = theme.text
        self.styles.padding = (0, 1)

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.08, self._advance)

    def _advance(self) -> None:
        self.frame_index = (self.frame_index + 1) % len(WAITING_FRAMES)

    def finish(self, result: str, is_error: bool, fact_index: int | None = None) -> None:
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
        if self.finished and self.result_text:
            lines.append(Text(truncate(self.result_text), style=self._theme.text))

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


ROBOT_ART = r"""
 ╭───────╮
 │ ◉   ◉ │
 │   ▾   │
 ╰┬─────┬╯
  ┴     ┴
"""


class Splash(Static):
    def __init__(self, theme: Theme = PICO_THEME) -> None:
        super().__init__(id="splash")
        self._theme = theme
        self.styles.color = theme.primary
        self.styles.padding = (1, 0, 1, 2)

    def render(self) -> Text:
        lines = [line for line in ROBOT_ART.splitlines() if line.strip()]
        art = Text("\n".join(lines), style=f"bold {self._theme.primary}")
        caption = Text("Pico", style=f"bold {self._theme.text}")
        return Text("\n").join([art, caption])


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
