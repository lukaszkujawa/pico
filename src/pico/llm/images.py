import base64
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageData:
    media_type: str
    base64: str


def encode_image(path: str) -> ImageData | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    detected = image_format(data[:12])
    if detected is None:
        return None
    return ImageData(media_type=f"image/{detected}", base64=base64.b64encode(data).decode())


def unreadable_stub(path: str) -> str:
    return f"[image at {path} is no longer readable]"


def image_format(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return None
