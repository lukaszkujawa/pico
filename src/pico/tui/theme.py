from dataclasses import dataclass

from textual.theme import Theme as TextualTheme


@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    surface: str
    primary: str
    accent: str
    text: str
    muted_text: str
    success: str
    warning: str
    error: str
    assistant: str
    tool_call: str
    tool_call_border: str
    idle: str
    running: str
    input_bar_bg: str
    input_bar_border: str
    input_prompt: str

    def to_textual(self) -> TextualTheme:
        return TextualTheme(
            name=self.name,
            primary=self.primary,
            accent=self.accent,
            background=self.background,
            surface=self.surface,
            foreground=self.text,
            success=self.success,
            warning=self.warning,
            error=self.error,
        )


PICO_THEME = Theme(
    name="pico",
    background="#12141c",
    surface="#1a1d29",
    primary="#7aa2f7",
    accent="#7aa2f7",
    text="#c8ccd4",
    muted_text="#6b7280",
    success="#4fd6a8",
    warning="#e0af68",
    error="#f7768e",
    assistant="#c8ccd4",
    tool_call="#9aa5ce",
    tool_call_border="#3b3f51",
    idle="#6b7280",
    running="#7aa2f7",
    input_bar_bg="#1a1d29",
    input_bar_border="#3b3f51",
    input_prompt="#7aa2f7",
)
