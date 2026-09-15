from textual.app import App, ComposeResult

from pico.tui.widgets import ThinkingBox, ToolCallBox


class ThinkingBoxHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ThinkingBox(box_id="0")


class ToolCallBoxHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ToolCallBox(box_id="1", name="search")


async def test_thinking_box_lifecycle() -> None:
    app = ThinkingBoxHarness()
    async with app.run_test() as pilot:
        box = app.query_one(ThinkingBox)
        assert box.render().plain == ""
        assert box.finished is False

        box.append_delta("Hello, ")
        box.append_delta("world!")
        await pilot.pause()
        assert box.render().plain == "Hello, world!"
        assert box.finished is False

        box.finish()
        await pilot.pause()
        assert box.finished is True
        assert box.render().plain == "Hello, world!"


async def test_tool_call_box_lifecycle_success() -> None:
    app = ToolCallBoxHarness()
    async with app.run_test() as pilot:
        box = app.query_one(ToolCallBox)
        assert box.name_label == "search"
        assert box.finished is False

        box.append_delta('{"q": "pico"}')
        await pilot.pause()
        assert "pico" in box.render().plain

        box.finish(is_error=False)
        await pilot.pause()
        assert box.finished is True
        assert box.is_error is False


async def test_tool_call_box_lifecycle_error() -> None:
    app = ToolCallBoxHarness()
    async with app.run_test() as pilot:
        box = app.query_one(ToolCallBox)

        box.append_delta("boom")
        box.finish(is_error=True)
        await pilot.pause()
        assert box.finished is True
        assert box.is_error is True
