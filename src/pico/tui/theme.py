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
    selection_bg: str
    success: str
    warning: str
    error: str
    assistant: str
    user: str
    tool_call: str
    tool_call_border: str
    input_prompt: str
    thinking_bg: str
    thinking: str
    waiting: str
    meter: str
    meter_empty: str

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
    background="#101010",
    surface="#1a1a1a",
    primary="#d6d6d6",
    accent="#d6d6d6",
    text="#c4c4c4",
    muted_text="#6e6e6e",
    selection_bg="#2f2f2f",
    success="#4fd6a8",
    warning="#e0af68",
    error="#f7768e",
    assistant="#c4c4c4",
    user="#f2f2f2",
    tool_call="#9a9a9a",
    tool_call_border="#3a3a3a",
    input_prompt="#8a8a8a",
    thinking_bg="#1a1a1a",
    thinking="#8a8a8a",
    waiting="#4fd6a8",
    meter="#d6d6d6",
    meter_empty="#3a3a3a",
)
