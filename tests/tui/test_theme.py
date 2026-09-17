import dataclasses
import re

import pytest
from textual.theme import Theme as TextualTheme

from pico.tui.theme import PICO_THEME

COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")

GREYSCALE_FIELDS = [
    "background",
    "surface",
    "primary",
    "accent",
    "text",
    "muted_text",
    "selection_bg",
    "assistant",
    "user",
    "tool_call",
    "tool_call_border",
    "input_prompt",
    "thinking_bg",
    "thinking",
    "meter",
    "meter_empty",
]

GLYPH_FIELDS = ["success", "warning", "error", "waiting"]

SEMANTIC_FIELDS = GREYSCALE_FIELDS + GLYPH_FIELDS


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


def _channels(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


@pytest.mark.parametrize("field", GREYSCALE_FIELDS)
def test_structural_colors_are_greyscale(field: str) -> None:
    red, green, blue = _channels(getattr(PICO_THEME, field))
    assert red == green == blue, f"{field} carries a hue"


@pytest.mark.parametrize("field", GLYPH_FIELDS)
def test_glyph_colors_keep_their_hue(field: str) -> None:
    red, green, blue = _channels(getattr(PICO_THEME, field))
    assert len({red, green, blue}) > 1, f"{field} lost its hue"


def test_every_theme_field_is_classified() -> None:
    fields = {field.name for field in dataclasses.fields(PICO_THEME)}
    assert fields - {"name"} == set(SEMANTIC_FIELDS)
