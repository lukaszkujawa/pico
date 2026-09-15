from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    ErrorOccurred,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.app import PicoApp
from pico.tui.widgets import ThinkingBox, ToolCallBox


async def test_app_renders_thinking_box_from_bus_events() -> None:
    bus = Bus()
    app = PicoApp(bus)
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="Hello, "))
        bus.publish(AssistantTextDelta(id="0", text="world!"))
        bus.publish(AssistantTextFinished(id="0"))
        bus.publish(RunFinished())

        await pilot.pause(0.2)

        boxes = app.query(ThinkingBox)
        assert len(boxes) == 1
        box = boxes.first()
        assert box.render().plain == "Hello, world!"
        assert box.finished is True


async def test_app_renders_tool_call_box_from_bus_events() -> None:
    bus = Bus()
    app = PicoApp(bus)
    async with app.run_test() as pilot:
        await pilot.pause()

        tool_call = ToolCall(id="1", name="search", arguments={"q": "pico"})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search"))
        bus.publish(ToolCallArgumentsDelta(id="1", arguments_delta='{"q": "pico"}'))
        bus.publish(
            ToolCallFinished(id="1", tool_call=tool_call, result="found it", is_error=False)
        )
        bus.publish(RunFinished())

        await pilot.pause(0.2)

        boxes = app.query(ToolCallBox)
        assert len(boxes) == 1
        box = boxes.first()
        assert box.name_label == "search"
        assert "found it" not in box.render().plain
        assert box.finished is True
        assert box.is_error is False


async def test_app_handles_multiple_boxes_and_error() -> None:
    bus = Bus()
    app = PicoApp(bus)
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="thinking"))
        bus.publish(AssistantTextFinished(id="0"))
        tool_call = ToolCall(id="1", name="search", arguments={})
        bus.publish(ToolCallStarted(id="1", name="search"))
        bus.publish(ToolCallFinished(id="1", tool_call=tool_call, result="oops", is_error=True))
        bus.publish(ErrorOccurred(message="something broke"))
        bus.publish(RunFinished(error="something broke"))

        await pilot.pause(0.2)

        thinking_boxes = app.query(ThinkingBox)
        tool_boxes = app.query(ToolCallBox)
        assert len(thinking_boxes) == 1
        assert len(tool_boxes) == 1
        assert tool_boxes.first().is_error is True
