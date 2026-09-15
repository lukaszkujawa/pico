import dataclasses
import re

import pytest
from textual.theme import Theme as TextualTheme

from pico.tui.theme import PICO_THEME

COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

SEMANTIC_FIELDS = [
    "background",
    "surface",
    "primary",
    "accent",
    "text",
    "muted_text",
    "success",
    "warning",
    "error",
    "assistant",
    "tool_call",
    "tool_call_border",
    "idle",
    "running",
    "input_bar_bg",
    "input_bar_border",
    "input_prompt",
]


def test_theme_is_frozen() -> None:
    assert dataclasses.is_dataclass(PICO_THEME)
    with pytest.raises(dataclasses.FrozenInstanceError):
        PICO_THEME.background = "#000000"  # type: ignore[misc]


def test_pico_theme_has_all_semantic_colors() -> None:
    for field in SEMANTIC_FIELDS:
        value = getattr(PICO_THEME, field)
        assert isinstance(value, str)
        assert COLOR_PATTERN.match(value), f"{field}={value!r} is not a valid color string"


def test_accent_hierarchy_has_single_primary() -> None:
    assert PICO_THEME.primary == PICO_THEME.accent


def test_error_and_success_colors_are_distinct() -> None:
    assert PICO_THEME.success != PICO_THEME.error
    assert PICO_THEME.success != PICO_THEME.warning
    assert PICO_THEME.error != PICO_THEME.warning


def test_to_textual_produces_textual_theme() -> None:
    textual_theme = PICO_THEME.to_textual()
    assert isinstance(textual_theme, TextualTheme)
    assert textual_theme.name == PICO_THEME.name
    assert textual_theme.primary == PICO_THEME.primary
