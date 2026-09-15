from textual.widgets import Input

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
from pico.tui.app import InputBar, PicoApp, StatusHeader
from pico.tui.messages import UserInputSubmitted
from pico.tui.widgets import AssistantPane, ErrorPane, ToolCallPane


async def test_app_renders_assistant_pane_from_bus_events() -> None:
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

        panes = app.query(AssistantPane)
        assert len(panes) == 1
        pane = panes.first()
        assert pane.render().plain == "Hello, world!"
        assert pane.finished is True


async def test_app_renders_tool_call_pane_from_bus_events() -> None:
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

        panes = app.query(ToolCallPane)
        assert len(panes) == 1
        pane = panes.first()
        assert pane.name_label == "search"
        assert "found it" not in pane.render().plain
        assert pane.finished is True
        assert pane.is_error is False


async def test_app_handles_multiple_panes_and_error() -> None:
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

        assistant_panes = app.query(AssistantPane)
        tool_panes = app.query(ToolCallPane)
        error_panes = app.query(ErrorPane)
        assert len(assistant_panes) == 1
        assert len(tool_panes) == 1
        assert tool_panes.first().is_error is True
        assert len(error_panes) == 1


async def test_header_reflects_run_state_transitions() -> None:
    bus = Bus()
    app = PicoApp(bus)
    async with app.run_test() as pilot:
        await pilot.pause()
        header = app.query_one(StatusHeader)
        assert header.status == "idle"

        bus.publish(RunStarted())
        await pilot.pause(0.2)
        assert header.status == "running"

        bus.publish(RunFinished())
        await pilot.pause(0.2)
        assert header.status == "idle"

        bus.publish(RunStarted())
        await pilot.pause(0.2)
        bus.publish(RunFinished(error="boom"))
        await pilot.pause(0.2)
        assert header.status == "error"


async def test_layout_renders_at_small_and_large_terminal_sizes() -> None:
    for size in [(80, 24), (160, 48)]:
        bus = Bus()
        app = PicoApp(bus)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            header = app.query_one(StatusHeader)
            input_bar = app.query_one(InputBar)
            assert header.region.width == size[0]
            assert input_bar.region.width == size[0]


async def test_input_bar_submission_emits_message_and_clears_field() -> None:
    bus = Bus()
    submitted: list[str] = []

    class TrackingApp(PicoApp):
        def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
            submitted.append(message.text)

    app = TrackingApp(bus)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", Input).focus()
        await pilot.pause()
        await pilot.press(*"hello pico")
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert submitted == ["hello pico"]
        assert app.query_one("#user-input", Input).value == ""
