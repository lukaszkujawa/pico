from pathlib import Path

import pytest

from pico.core.actions.view import VIEW_IMAGE_CAP_BYTES, view_image_tool
from pico.core.tools import ToolError
from pico.llm.images import image_format
from tests.image_fixtures import TINY_PNG


def _view(path: Path) -> str:
    return view_image_tool().execute({"path": str(path)})


def test_a_valid_png_returns_the_one_line_handle(tmp_path: Path) -> None:
    path = tmp_path / "shot.png"
    path.write_bytes(TINY_PNG)

    assert _view(path) == f"viewing {path} (png, {len(TINY_PNG)} bytes)"


def test_format_comes_from_magic_bytes_not_the_extension(tmp_path: Path) -> None:
    path = tmp_path / "shot.jpg"
    path.write_bytes(TINY_PNG)

    assert "(png," in _view(path)


def test_a_missing_path_names_the_path(tmp_path: Path) -> None:
    path = tmp_path / "nope.png"

    with pytest.raises(ToolError, match=f"could not read {path}"):
        _view(path)


def test_a_non_image_file_names_the_path(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("just text")

    with pytest.raises(ToolError, match=f"{path} is not an image"):
        _view(path)


def test_an_oversized_image_says_to_downscale(tmp_path: Path) -> None:
    path = tmp_path / "huge.png"
    path.write_bytes(TINY_PNG + b"\x00" * VIEW_IMAGE_CAP_BYTES)

    with pytest.raises(ToolError, match="downscale it with shell first"):
        _view(path)


@pytest.mark.parametrize(
    ("header", "detected"),
    [
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff\xe0", "jpeg"),
        (b"GIF87a", "gif"),
        (b"GIF89a", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "webp"),
        (b"RIFF\x00\x00\x00\x00WAVE", None),
        (b"plain text", None),
        (b"", None),
    ],
)
def test_image_format_detects_by_magic_bytes(header: bytes, detected: str | None) -> None:
    assert image_format(header) == detected
