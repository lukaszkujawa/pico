from pathlib import Path

from pico.llm.images import encode_image
from tests.image_fixtures import TINY_PNG


def test_encode_image_returns_none_for_a_non_image_file(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("just text")

    assert encode_image(str(path)) is None


def test_encode_image_returns_none_for_a_missing_file(tmp_path: Path) -> None:
    assert encode_image(str(tmp_path / "gone.png")) is None


def test_encode_image_reports_the_media_type_from_magic_bytes(tmp_path: Path) -> None:
    path = tmp_path / "shot.jpg"
    path.write_bytes(TINY_PNG)

    encoded = encode_image(str(path))

    assert encoded is not None
    assert encoded.media_type == "image/png"
