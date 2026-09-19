import os
from collections.abc import Mapping

from pico.core.actions.arguments import require
from pico.core.tools import Tool, ToolError
from pico.llm.images import image_format
from pico.llm.types import ToolSpec

VIEW_IMAGE_CAP_BYTES = 4_000_000

VIEW_IMAGE_SPEC = ToolSpec(
    name="view_image",
    description=(
        "Look at an image file (png, jpeg, gif, or webp) at the given path. "
        "Only the most recently viewed image stays in sight, so record what you "
        "observe with note before moving on."
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


def view_image_tool() -> Tool:
    def execute(arguments: Mapping[str, object]) -> str:
        path = require(arguments, "path", str)
        try:
            with open(path, "rb") as handle:
                header = handle.read(12)
                size = os.fstat(handle.fileno()).st_size
        except OSError as error:
            raise ToolError(f"could not read {path}: {error}") from error
        detected = image_format(header)
        if detected is None:
            raise ToolError(f"{path} is not an image; view_image reads png, jpeg, gif, and webp")
        if size > VIEW_IMAGE_CAP_BYTES:
            raise ToolError(
                f"{path} is {size} bytes, over the {VIEW_IMAGE_CAP_BYTES} byte limit; "
                "downscale it with shell first"
            )
        return f"viewing {path} ({detected}, {size} bytes)"

    return Tool(spec=VIEW_IMAGE_SPEC, execute=execute)
