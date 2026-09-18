from datetime import datetime

import pytest
from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult

from pico.tui import widgets
from pico.tui.commands import Completion, Row
from pico.tui.theme import PICO_THEME
from pico.tui.widgets import (
    ERROR_GLYPH,
    SUCCESS_GLYPH,
    WAITING_FRAMES,
    ActivityStrip,
    AnswerPane,
    AssistantPane,
    CommandMenu,
    ContextMeter,
    ElapsedTimer,
    ErrorPane,
    RequestCounter,
    Splash,
    ThinkingPane,
    ToolCallPane,
    UserPane,
    WaitingIndicator,
    format_elapsed,
    format_thousands,
)
from tests.conftest import settle


class AssistantPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield AssistantPane(pane_id="0")


class AnswerPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield AnswerPane(pane_id="0")


class ThinkingPaneHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ThinkingPane(pane_id="0")
        yield AssistantPane(pane_id="1")


class WaitingIndicatorHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield WaitingIndicator()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ElapsedTimerHarness(App[None]):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock

    def compose(self) -> ComposeResult:
        yield ElapsedTimer(clock=self._clock)


class ActivityStripHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield ActivityStrip(wall=lambda: datetime(2026, 9, 18, 14, 5))


async def test_activity_strip_stop_swaps_readouts_for_a_worked_summary() -> None:
    app = ActivityStripHarness()
    async with app.run_test() as pilot:
        strip = app.query_one(ActivityStrip)
        strip.start()
        await pilot.pause()
        assert strip.summary.display is False

        strip.timer.elapsed = 72
        strip.stop()
        await pilot.pause()

        assert strip.summary.display is True
        assert strip.query_one("#activity-live").display is False
        assert str(strip.summary.render()) == "* Worked for 1m12s - done 14:05"

        strip.start()
        await pilot.pause()

        assert strip.summary.display is False
        assert strip.query_one("#activity-live").display is True


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

        pane.append_delta("Hello, ")
        pane.append_delta("world!")
        await pilot.pause()
        assert pane.render().plain == "Hello, world!"


async def test_answer_pane_renders_without_box() -> None:
    app = AnswerPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AnswerPane)
        await pilot.pause()

        assert pane.styles.border.top[0] == ""


async def test_answer_pane_accumulates_content_deltas_while_streaming() -> None:
    app = AnswerPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AnswerPane)
        await pilot.pause()

        pane.append_delta("The final ")
        pane.append_delta("answer is 42.")
        await pilot.pause()

        assert "The final answer is 42." in pane.render().plain
        assert pane.settled is False


async def test_answer_pane_accepted_renders_content_with_success_marker() -> None:
    app = AnswerPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AnswerPane)
        await pilot.pause()

        pane.settle(content="42", accepted=True, reason=None, verify=None)
        await pilot.pause()

        assert pane.settled is True
        assert pane.accepted is True
        assert SUCCESS_GLYPH in pane.render().plain
        assert "42" in pane.render().plain


async def test_answer_pane_accepted_with_verification_shows_verify_badge_not_body_text() -> None:
    app = AnswerPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AnswerPane)
        await pilot.pause()

        pane.settle(content="42", accepted=True, reason=None, verify="pytest")
        await pilot.pause()

        rendered = pane.render().plain
        assert "pytest" in rendered
        assert "verified:" not in "42"


async def test_answer_pane_rejected_renders_reason_distinct_from_accepted_and_error() -> None:
    app = AnswerPaneHarness()
    error_app = ErrorPaneHarness()
    async with app.run_test() as pilot, error_app.run_test() as error_pilot:
        pane = app.query_one(AnswerPane)
        await pilot.pause()

        pane.settle(
            content="",
            accepted=False,
            reason="unknown fact citation(s): [3]",
            verify=None,
        )
        await pilot.pause()

        assert pane.settled is True
        assert pane.accepted is False
        rendered = pane.render().plain
        assert "unknown fact citation(s): [3]" in rendered
        assert SUCCESS_GLYPH not in rendered
        assert ERROR_GLYPH not in rendered

        error_pane = error_app.query_one(ErrorPane)
        await error_pilot.pause()
        assert pane.styles.border.top[0] != error_pane.styles.border.top[0]


async def test_thinking_pane_lifecycle() -> None:
    app = ThinkingPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ThinkingPane)
        assert pane.render().plain == ""

        pane.append_delta("pondering ")
        pane.append_delta("deeply")
        await pilot.pause()
        assert pane.render().plain == "pondering deeply"


async def test_thinking_pane_wraps_long_text_and_grows_taller_than_one_line() -> None:
    app = ThinkingPaneHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(ThinkingPane)
        long_text = "word " * 100
        pane.append_delta(long_text)
        await pilot.pause()

        assert pane.size.height > 1
        assert pane.render().plain == long_text


async def test_assistant_pane_wraps_long_text_and_grows_taller_than_one_line() -> None:
    app = AssistantPaneHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(AssistantPane)
        long_text = "word " * 100
        pane.append_delta(long_text)
        await pilot.pause()

        assert pane.size.height > 1
        assert pane.render().plain == long_text


async def test_answer_pane_wraps_long_text_and_grows_taller_than_one_line() -> None:
    app = AnswerPaneHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(AnswerPane)
        long_text = "word " * 100
        pane.append_delta(long_text)
        await pilot.pause()

        assert pane.size.height > 1
        assert long_text in pane.render().plain


async def test_pane_with_explicit_newline_still_breaks_there() -> None:
    app = ThinkingPaneHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(ThinkingPane)
        pane.append_delta("line one\nline two")
        await pilot.pause()

        assert pane.size.height == 2


async def test_short_pane_stays_height_one() -> None:
    app = ThinkingPaneHarness()
    async with app.run_test(size=(80, 24)) as pilot:
        pane = app.query_one(ThinkingPane)
        pane.append_delta("short")
        await pilot.pause()

        assert pane.size.height == 1


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
        await settle(
            pilot,
            lambda: pane.render().plain != first_frame,
            "the pending icon advances to another frame",
        )


async def test_tool_call_pane_animation_stops_once_finished() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)
        await pilot.pause()
        assert pane._timer is not None  # pyright: ignore[reportPrivateUsage]

        pane.finish(result="done", is_error=False)
        await pilot.pause()

        assert pane._timer is None  # pyright: ignore[reportPrivateUsage]
        frame_after_finish = pane.render().plain
        await pilot.pause()
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


async def test_tool_call_pane_accumulates_argument_deltas() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.append_arguments_delta('{"path":')
        pane.append_arguments_delta(' "a.txt"}')
        await pilot.pause()

        assert '{"path": "a.txt"}' in pane.render().plain


async def test_tool_call_pane_accumulates_result_deltas() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.append_result_delta("line one\n")
        pane.append_result_delta("line two\n")
        await pilot.pause()

        assert "line one\nline two\n" in pane.render().plain


async def test_tool_call_pane_finish_replaces_streamed_arguments_and_result() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.append_arguments_delta('{"q": "partial')
        pane.append_result_delta("partial output")
        await pilot.pause()

        pane.finish(result="final output", is_error=False, arguments='{"q": "pico"}')
        await pilot.pause()

        rendered = pane.render().plain
        assert "partial" not in rendered
        assert "final output" in rendered
        assert '{"q": "pico"}' in rendered


async def test_tool_call_pane_truncates_finished_result_after_streaming() -> None:
    app = ToolCallPaneHarness()
    long_result = "y" * 400
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)

        pane.append_result_delta("y" * 50)
        pane.finish(result=long_result, is_error=False)
        await pilot.pause()

        rendered = pane.render().plain
        assert long_result not in rendered
        assert "y" * 300 in rendered
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
        assert indicator.render().plain.strip() == ""

        indicator.start()
        await pilot.pause()
        assert indicator.running is True
        assert indicator.render().plain in WAITING_FRAMES

        indicator.stop()
        await pilot.pause()
        assert indicator.running is False
        assert indicator.render().plain.strip() == ""


async def test_waiting_indicator_animates_over_ticks() -> None:
    app = WaitingIndicatorHarness()
    async with app.run_test() as pilot:
        indicator = app.query_one(WaitingIndicator)
        indicator.start()
        await pilot.pause()

        first_frame = indicator.render().plain
        await settle(
            pilot,
            lambda: indicator.render().plain != first_frame,
            "the waiting indicator advances to another frame",
        )

        indicator.stop()


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (7, "7s"), (59, "59s"), (60, "1m00s"), (72, "1m12s"), (605, "10m05s")],
)
def test_format_elapsed_switches_to_minutes_at_the_boundary(seconds: int, expected: str) -> None:
    assert format_elapsed(seconds) == expected


async def test_elapsed_timer_climbs_while_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widgets, "ELAPSED_TICK_SECONDS", 0.01)
    clock = FakeClock()

    app = ElapsedTimerHarness(clock)
    async with app.run_test() as pilot:
        timer = app.query_one(ElapsedTimer)
        assert timer.render().plain == "0s"

        timer.start()
        clock.now = 7.0
        await settle(pilot, lambda: timer.render().plain == "7s", "the timer reaches 7s")

        clock.now = 72.0
        await settle(pilot, lambda: timer.render().plain == "1m12s", "the timer reaches 1m12s")

        timer.stop()


async def test_elapsed_timer_freezes_final_value_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widgets, "ELAPSED_TICK_SECONDS", 0.01)
    clock = FakeClock()

    app = ElapsedTimerHarness(clock)
    async with app.run_test() as pilot:
        timer = app.query_one(ElapsedTimer)
        timer.start()
        clock.now = 59.0
        await settle(pilot, lambda: timer.render().plain == "59s", "the timer reaches 59s")

        timer.stop()
        clock.now = 300.0
        await pilot.pause()

        assert timer.running is False
        assert timer.render().plain == "59s"


def test_splash_renders_session_id_when_given() -> None:
    assert "session abc123" in Splash("abc123").render().plain


def test_splash_omits_session_line_when_id_is_empty() -> None:
    assert "session " not in Splash().render().plain


def test_splash_art_shares_rows_with_right_column_text() -> None:
    lines = Splash("abc123", "~/projects/pico").render().split("\n")
    plain_lines = [line.plain for line in lines]
    assert any(widgets.LOGO_TOP in line and "PICO" in line for line in plain_lines)
    assert any(widgets.LOGO_MID in line and "session abc123" in line for line in plain_lines)
    assert any(widgets.LOGO_BOTTOM in line and "~/projects/pico" in line for line in plain_lines)


def test_splash_label_is_bold_theme_text() -> None:
    lines = Splash("abc123").render().split("\n")
    label_line = next(line for line in lines if "PICO" in line.plain)
    start = label_line.plain.index("PICO")
    style = label_line.get_style_at_offset(Console(), start)
    assert style.bold
    assert style.color is not None
    assert style.color.name == PICO_THEME.text


def test_splash_tagline_is_muted_and_not_italic() -> None:
    lines = Splash("abc123").render().split("\n")
    tagline_line = next(line for line in lines if widgets.TAGLINE in line.plain)
    start = tagline_line.plain.index(widgets.TAGLINE)
    style = tagline_line.get_style_at_offset(Console(), start)
    assert not style.italic
    assert style.color is not None
    assert style.color.name == PICO_THEME.muted_text


def test_splash_local_directory_renders_with_home_shorthand() -> None:
    lines = Splash("abc123", "~/projects/pico").render().split("\n")
    directory_line = next(line for line in lines if "~/projects/pico" in line.plain)
    start = directory_line.plain.index("~/projects/pico")
    style = directory_line.get_style_at_offset(Console(), start)
    assert style.color is not None
    assert style.color.name == PICO_THEME.muted_text


def test_splash_right_column_centers_against_four_line_art_with_session() -> None:
    lines = Splash("abc123", "~/projects/pico").render().split("\n")
    assert len(lines) == widgets.LOGO_HEIGHT
    right_texts = [line.plain[len(widgets.LOGO_TOP) + len(widgets.COLUMN_GAP) :] for line in lines]
    assert right_texts == [
        "PICO",
        widgets.TAGLINE,
        "session abc123",
        "~/projects/pico",
    ]


def test_splash_right_column_centers_against_four_line_art_without_session() -> None:
    lines = Splash("", "~/projects/pico").render().split("\n")
    assert len(lines) == widgets.LOGO_HEIGHT
    right_texts = [line.plain[len(widgets.LOGO_TOP) + len(widgets.COLUMN_GAP) :] for line in lines]
    assert right_texts == [
        "PICO",
        widgets.TAGLINE,
        "~/projects/pico",
        "",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (999, "999"), (1000, "1k"), (1500, "1.5k"), (8192, "8.2k"), (32000, "32k")],
)
def test_format_thousands_compacts_large_counts(value: int, expected: str) -> None:
    assert format_thousands(value) == expected


class ContextMeterHarness(App[None]):
    def __init__(self, context_size: int = 1000) -> None:
        super().__init__()
        self._context_size = context_size

    def compose(self) -> ComposeResult:
        yield ContextMeter(self._context_size)


async def test_context_meter_starts_empty_and_fills_with_use() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        assert meter.render().plain == f"{widgets.METER_EMPTY * widgets.METER_WIDTH} 0/1k"

        meter.used = 500
        await pilot.pause()
        filled = widgets.METER_FILLED * 5 + widgets.METER_EMPTY * 5
        assert meter.render().plain == f"{filled} 500/1k"


async def test_context_meter_clamps_and_turns_error_coloured_over_budget() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)

        meter.used = 400
        await pilot.pause()
        assert meter.render().get_style_at_offset(Console(), 0).color is not None
        assert meter.render().get_style_at_offset(Console(), 0).color.name == PICO_THEME.meter  # type: ignore[union-attr]

        meter.used = 4000
        await pilot.pause()
        assert meter.ratio == 1.0
        assert meter.render().plain.startswith(widgets.METER_FILLED * widgets.METER_WIDTH)
        assert meter.render().get_style_at_offset(Console(), 0).color.name == PICO_THEME.error  # type: ignore[union-attr]


async def test_context_meter_with_no_context_size_stays_empty() -> None:
    app = ContextMeterHarness(context_size=0)
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        meter.used = 100
        await pilot.pause()

        assert meter.ratio == 0.0


def _bar_color(meter: ContextMeter) -> str | None:
    color = meter.render().get_style_at_offset(Console(), 0).color
    return None if color is None else color.name


async def test_context_meter_warning_band_when_pressure_is_active() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        meter.used = 400
        meter.pressure = True
        await pilot.pause()

        assert _bar_color(meter) == PICO_THEME.meter_warning
        assert "400/1k" in meter.render().plain


async def test_context_meter_dying_shows_the_error_band_at_low_usage() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        meter.used = 100
        meter.dying = True
        await pilot.pause()

        assert _bar_color(meter) == PICO_THEME.error


async def test_context_meter_most_severe_band_wins() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        meter.used = 950
        meter.pressure = True
        await pilot.pause()
        assert _bar_color(meter) == PICO_THEME.error

        meter.used = 400
        meter.dying = True
        await pilot.pause()
        assert _bar_color(meter) == PICO_THEME.error


async def test_context_meter_reset_returns_the_band_to_normal() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        meter.used = 400
        meter.pressure = True
        meter.dying = True
        await pilot.pause()

        meter.reset()
        await pilot.pause()
        assert meter.used == 0
        assert meter.pressure is False
        assert meter.dying is False

        meter.used = 400
        await pilot.pause()
        assert _bar_color(meter) == PICO_THEME.meter


class RequestCounterHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield RequestCounter()


class BudgetedRequestCounterHarness(App[None]):
    def compose(self) -> ComposeResult:
        yield RequestCounter(budget=50)


async def test_request_counter_renders_progress_against_a_known_budget() -> None:
    app = BudgetedRequestCounterHarness()
    async with app.run_test() as pilot:
        counter = app.query_one(RequestCounter)
        assert counter.render().plain == "0/50 req"

        counter.requests = 7
        await pilot.pause()
        assert counter.render().plain == "7/50 req"

        counter.reset()
        await pilot.pause()
        assert counter.render().plain == "0/50 req"


async def test_request_counter_increments_and_resets() -> None:
    app = RequestCounterHarness()
    async with app.run_test() as pilot:
        counter = app.query_one(RequestCounter)
        assert counter.render().plain == "0 req"

        counter.increment()
        counter.increment()
        await pilot.pause()
        assert counter.render().plain == "2 req"

        counter.reset()
        await pilot.pause()
        assert counter.render().plain == "0 req"


def _hues(text: Text) -> set[str]:
    console = Console()
    names: set[str] = set()
    for offset in range(len(text.plain)):
        color = text.get_style_at_offset(console, offset).color
        if color is not None and color.triplet is not None:
            red, green, blue = color.triplet
            if len({red, green, blue}) > 1:
                names.add(color.name)
    return names


async def test_answer_pane_colors_only_its_marker() -> None:
    app = AnswerPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(AnswerPane)
        pane.append_delta("a streaming answer")
        await pilot.pause()
        assert _hues(pane.render()) == set()

        pane.settle(content="42", accepted=True, reason=None, verify="checked")
        await pilot.pause()
        assert _hues(pane.render()) == {PICO_THEME.success}

        pane.settle(content="42", accepted=False, reason="try again", verify=None)
        await pilot.pause()
        assert _hues(pane.render()) == {PICO_THEME.warning}


async def test_tool_call_pane_colors_only_its_glyph() -> None:
    app = ToolCallPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ToolCallPane)
        await pilot.pause()
        assert _hues(pane.render()) == {PICO_THEME.waiting}

        pane.finish(result="ok", is_error=False, fact_index=3)
        await pilot.pause()
        assert _hues(pane.render()) == {PICO_THEME.success}

        pane.finish(result="boom", is_error=True)
        await pilot.pause()
        assert _hues(pane.render()) == {PICO_THEME.error}


async def test_error_pane_colors_only_its_glyph() -> None:
    app = ErrorPaneHarness()
    async with app.run_test() as pilot:
        pane = app.query_one(ErrorPane)
        await pilot.pause()

        assert _hues(pane.render()) == {PICO_THEME.error}
        assert pane.styles.border.top[1].hex.lower() == PICO_THEME.tool_call_border


def test_splash_colors_only_the_prompt_and_cursor() -> None:
    splash = Splash("abc123", "~/projects/pico")
    assert _hues(splash.render()) == {PICO_THEME.success}


@pytest.mark.parametrize(
    "pane",
    [AssistantPane(pane_id="0"), ThinkingPane(pane_id="0"), UserPane(text="hello")],
)
def test_conversation_panes_render_without_hue(
    pane: AssistantPane | ThinkingPane | UserPane,
) -> None:
    if not isinstance(pane, UserPane):
        pane.append_delta("some text")
    assert _hues(pane.render()) == set()


async def test_stats_strip_readouts_are_monochrome_until_over_budget() -> None:
    app = ContextMeterHarness()
    async with app.run_test() as pilot:
        meter = app.query_one(ContextMeter)
        meter.used = 500
        await pilot.pause()
        assert _hues(meter.render()) == set()

        meter.used = 950
        await pilot.pause()
        assert _hues(meter.render()) == {PICO_THEME.error}


def test_command_menu_with_no_completion_renders_nothing_and_accepts_nothing() -> None:
    menu = CommandMenu()

    assert menu.render().plain == ""
    assert menu.accept() is None
    assert menu.selection is None

    menu.move(1)

    assert menu.selected == 0


def _selected_backgrounds(menu: CommandMenu) -> list[str]:
    rendered = menu.render()
    return [
        str(span.style)
        for span in rendered.spans
        if isinstance(span.style, str) and span.style.startswith("on ")
    ]


def test_command_menu_highlights_the_only_row_when_one_candidate_remains() -> None:
    menu = CommandMenu()
    menu.show(Completion(rows=(Row(label="model", hint="switch the model"),)))

    assert _selected_backgrounds(menu) == [f"on {PICO_THEME.selection_bg}"]


def test_command_menu_moves_the_highlight_with_the_selection() -> None:
    menu = CommandMenu()
    menu.show(Completion(rows=(Row(label="model"), Row(label="quit"))))
    first, second = menu.render().plain.split("\n")

    highlighted = next(span for span in menu.render().spans if str(span.style).startswith("on "))
    assert highlighted.start == 0
    assert highlighted.end == len(first)

    menu.move(1)
    highlighted = next(span for span in menu.render().spans if str(span.style).startswith("on "))
    assert highlighted.start == len(first) + 1
    assert highlighted.end == len(first) + 1 + len(second)
