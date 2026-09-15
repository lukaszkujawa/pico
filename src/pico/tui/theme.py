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
    thinking: str
    tool_call: str

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
    background="#0d1117",
    surface="#161b22",
    primary="#58a6ff",
    accent="#bc8cff",
    text="#c9d1d9",
    muted_text="#8b949e",
    success="#3fb950",
    warning="#d29922",
    error="#f85149",
    thinking="#58a6ff",
    tool_call="#bc8cff",
)
