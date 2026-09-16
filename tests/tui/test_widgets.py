from textual.app import App, ComposeResult

from pico.tui.widgets import (
    ERROR_GLYPH,
    SUCCESS_GLYPH,
    AnswerPane,
    AssistantPane,
    ErrorPane,
    ThinkingPane,
    ToolCallPane,
    WaitingIndicator,
)


class AssistantPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield AssistantPane(pane_id="0")


class AnswerPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield AnswerPane(pane_id="0", content="The final answer is 42.")


class ThinkingPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ThinkingPane(pane_id="0")
        yield AssistantPane(pane_id="1")


class WaitingIndicatorHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield WaitingIndicator()


class ToolCallPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ToolCallPane(pane_id="1", name="search", arguments='{"q": "pico"}')


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


async def test_answer_pane_renders_content_without_box() -> None:
    app = AnswerPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AnswerPane)
        await pilot.pause()

        assert "The final answer is 42." in pane.render().plain
        assert pane.styles.border.top[0] == ""


async def test_thinking_pane_lifecycle() -> None:
    app = ThinkingPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ThinkingPane)
        assert pane.render().plain == ""
        assert pane.finished is False

        pane.append_delta("pondering ")
        pane.append_delta("deeply")
        await pilot.pause()
        assert pane.render().plain == "pondering deeply"
        assert pane.finished is False

        pane.finish()
        await pilot.pause()
        assert pane.finished is True
        assert pane.render().plain == "pondering deeply"


async def test_thinking_pane_background_differs_from_assistant_pane() -> None:
    app = ThinkingPaneHarness()
    async with app.run_test():
        thinking_pane = app.query_one(ThinkingPane)
        assistant_pane = app.query_one(AssistantPane)
        assert thinking_pane.styles.background != assistant_pane.styles.background


async def test_tool_call_pane_lifecycle_success() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)
        assert pane.name_label == "search"
        assert pane.finished is False
        assert "pico" in pane.render().plain

        pane.finish(result="found it", is_error=False)
        await pilot.pause()
        assert pane.finished is True
        assert pane.is_error is False
        assert SUCCESS_GLYPH in pane.render().plain
        assert ERROR_GLYPH not in pane.render().plain
        assert "found it" in pane.render().plain


async def test_tool_call_pane_pending_icon_animates_over_ticks() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)
        await pilot.pause()

        first_frame = pane.render().plain
        await pilot.pause(0.1)
        second_frame = pane.render().plain
        await pilot.pause(0.1)
        third_frame = pane.render().plain

        assert len({first_frame, second_frame, third_frame}) > 1


async def test_tool_call_pane_animation_stops_once_finished() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)
        await pilot.pause()

        pane.finish(result="done", is_error=False)
        await pilot.pause()

        frame_after_finish = pane.render().plain
        await pilot.pause(0.2)
        assert pane.render().plain == frame_after_finish


async def test_tool_call_pane_lifecycle_error() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.finish(result="tool crashed", is_error=True)
        await pilot.pause()
        assert pane.finished is True
        assert pane.is_error is True
        assert ERROR_GLYPH in pane.render().plain
        assert SUCCESS_GLYPH not in pane.render().plain
        assert "tool crashed" in pane.render().plain


async def test_tool_call_pane_truncates_long_result() -> None:
    app = ToolCallPaneHarness()
    long_result = "x" * 400
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.finish(result=long_result, is_error=False)
        await pilot.pause()

        rendered = pane.render().plain
        assert long_result not in rendered
        assert "x" * 300 in rendered
        assert "100 more chars" in rendered


async def test_tool_call_pane_shows_fact_index_on_success() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.finish(result="ok", is_error=False, fact_index=3)
        await pilot.pause()

        assert "fact #3" in pane.render().plain


async def test_tool_call_pane_has_no_fact_index_on_error() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.finish(result="boom", is_error=True)
        await pilot.pause()

        assert "fact #" not in pane.render().plain


async def test_success_and_error_are_distinguishable_beyond_color() -> None:
    success_app = ToolCallPaneHarness()
    error_app = ToolCallPaneHarness()
    async with success_app.run_test() as success_pilot, error_app.run_test() as error_pilot:
        success_pane = success_app.query_one(ToolCallPane)
        error_pane = error_app.query_one(ToolCallPane)

        success_pane.finish(result="ok", is_error=False)
        error_pane.finish(result="boom", is_error=True)
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


async def test_waiting_indicator_start_and_stop_toggle_state() -> None:
    app = WaitingIndicatorHarness()
    async with app.run_test() as pilot:
        indicator = app.query_one(WaitingIndicator)
        assert indicator.running is False
        assert indicator.display is False

        indicator.start()
        await pilot.pause()
        assert indicator.running is True
        assert indicator.display is True

        indicator.stop()
        await pilot.pause()
        assert indicator.running is False
        assert indicator.display is False


async def test_waiting_indicator_animates_over_ticks() -> None:
    app = WaitingIndicatorHarness()
    async with app.run_test() as pilot:
        indicator = app.query_one(WaitingIndicator)
        indicator.start()
        await pilot.pause()

        first_frame = indicator.render().plain
        await pilot.pause(0.1)
        second_frame = indicator.render().plain
        await pilot.pause(0.1)
        third_frame = indicator.render().plain

        indicator.stop()

        assert len({first_frame, second_frame, third_frame}) > 1
