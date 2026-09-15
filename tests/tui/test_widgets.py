from textual.app import App, ComposeResult

from pico.tui.widgets import ERROR_GLYPH, SUCCESS_GLYPH, AssistantPane, ErrorPane, ToolCallPane


class AssistantPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield AssistantPane(pane_id="0")


class ToolCallPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ToolCallPane(pane_id="1", name="search")


class ErrorPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ErrorPane(message="something broke")


async def test_assistant_pane_lifecycle() -> None:
    app = AssistantPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AssistantPane)
        assert pane.render().plain == ""
        assert pane.finished is False

        pane.append_delta("Hello, ")
        pane.append_delta("world!")
        await pilot.pause()
        assert pane.render().plain == "Hello, world!"
        assert pane.finished is False

        pane.finish()
        await pilot.pause()
        assert pane.finished is True
        assert pane.render().plain == "Hello, world!"


async def test_tool_call_pane_lifecycle_success() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)
        assert pane.name_label == "search"
        assert pane.finished is False

        pane.append_delta('{"q": "pico"}')
        await pilot.pause()
        assert "pico" in pane.render().plain

        pane.finish(is_error=False)
        await pilot.pause()
        assert pane.finished is True
        assert pane.is_error is False
        assert SUCCESS_GLYPH in pane.render().plain
        assert ERROR_GLYPH not in pane.render().plain


async def test_tool_call_pane_lifecycle_error() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.append_delta("boom")
        pane.finish(is_error=True)
        await pilot.pause()
        assert pane.finished is True
        assert pane.is_error is True
        assert ERROR_GLYPH in pane.render().plain
        assert SUCCESS_GLYPH not in pane.render().plain


async def test_success_and_error_are_distinguishable_beyond_color() -> None:
    success_app = ToolCallPaneHarness()
    error_app = ToolCallPaneHarness()
    async with success_app.run_test() as success_pilot, error_app.run_test() as error_pilot:
        success_pane = success_app.query_one(ToolCallPane)
        error_pane = error_app.query_one(ToolCallPane)

        success_pane.finish(is_error=False)
        error_pane.finish(is_error=True)
        await success_pilot.pause()
        await error_pilot.pause()

        assert SUCCESS_GLYPH in success_pane.render().plain
        assert ERROR_GLYPH in error_pane.render().plain
        assert success_pane.render().plain != error_pane.render().plain


async def test_error_pane_renders_message() -> None:
    app = ErrorPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ErrorPane)
        await pilot.pause()
        assert "something broke" in pane.render().plain
        assert ERROR_GLYPH in pane.render().plain
