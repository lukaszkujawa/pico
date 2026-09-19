import queue
import re

import pytest
from textual.containers import Vertical, VerticalScroll

from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    BusEvent,
    ErrorOccurred,
    GenerationCompleted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.app import InputBar, PicoApp
from pico.tui.widgets import (
    ActivityStrip,
    AssistantPane,
    ContextMeter,
    ElapsedTimer,
    RequestCounter,
    StatsStrip,
    ThinkingPane,
    TokenCounter,
    ToolCallPane,
    WaitingIndicator,
)
from tests.conftest import settle
from tests.tui.app_fixtures import RecordingSessionHandle, submit


async def test_first_pane_create_keeps_waiting_indicator_running() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        await settle(pilot, lambda: len(app.query(ThinkingPane)) == 1, "the thinking pane mounts")

        assert app.query_one(WaitingIndicator).running is True


async def test_error_before_any_pane_stops_waiting_indicator() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(ErrorOccurred(message="boom"))
        bus.publish(RunFinished(error="boom"))
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the waiting indicator stops",
        )


async def test_streaming_deltas_climb_the_token_estimate() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        counter = app.query_one(TokenCounter)
        assert counter.render().plain == "~0 tokens"

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        bus.publish(AssistantThinkingDelta(id="0", text="t" * 40))
        await settle(pilot, lambda: counter.tokens == 10, "the thinking delta is counted")

        bus.publish(AssistantTextStarted(id="1"))
        bus.publish(AssistantTextDelta(id="1", text="x" * 80))
        await settle(pilot, lambda: counter.tokens == 30, "the text delta adds to the estimate")

        assert counter.render().plain == "~30 tokens"


async def test_generation_completed_snaps_estimate_to_real_count() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        counter = app.query_one(TokenCounter)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="x" * 400))
        await settle(
            pilot, lambda: counter.render().plain == "~100 tokens", "the estimate accumulates"
        )

        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=37))
        await settle(
            pilot,
            lambda: counter.render().plain == "37 tokens",
            "the estimate snaps to the real count",
        )


async def test_estimates_continue_on_top_of_the_reconciled_baseline() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        counter = app.query_one(TokenCounter)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="x" * 400))
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=37))
        await settle(
            pilot, lambda: counter.render().plain == "37 tokens", "the first count reconciles"
        )

        bus.publish(AssistantTextDelta(id="0", text="y" * 40))
        await settle(
            pilot,
            lambda: counter.render().plain == "~47 tokens",
            "later deltas estimate on top of the baseline",
        )

        bus.publish(GenerationCompleted(prompt_tokens=300, completion_tokens=11))
        await settle(
            pilot, lambda: counter.render().plain == "48 tokens", "the second count reconciles"
        )


async def test_missing_token_counts_leave_the_estimate_alone() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        counter = app.query_one(TokenCounter)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="x" * 400))
        bus.publish(GenerationCompleted())
        await settle(
            pilot,
            lambda: counter.render().plain == "~100 tokens",
            "the estimate survives a count-free completion",
        )


async def test_new_turn_resets_the_token_counter() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        counter = app.query_one(TokenCounter)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="x" * 400))
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=37))
        bus.publish(RunFinished())
        await settle(pilot, lambda: counter.tokens == 37, "the first turn reconciles")

        bus.publish(RunStarted())
        await settle(pilot, lambda: counter.tokens == 0, "the new turn resets the counter")

        assert counter.render().plain == "~0 tokens"


async def test_submitting_input_starts_spinner_elapsed_and_tokens_together() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        activity = app.query_one(ActivityStrip)
        assert activity.display is False
        assert app.query_one(WaitingIndicator).running is False

        await submit(app, pilot)

        assert activity.display is True
        assert app.query_one(WaitingIndicator).running is True
        assert app.query_one(ElapsedTimer).running is True
        assert app.query_one(ElapsedTimer).elapsed == 0
        assert app.query_one(TokenCounter).render().plain == "~0 tokens"


@pytest.mark.parametrize(
    "ending",
    [
        [RunFinished()],
        [RunCancelled()],
        [ErrorOccurred(message="boom"), RunFinished(error="boom")],
    ],
)
async def test_run_ending_replaces_readouts_with_a_worked_summary(
    ending: list[BusEvent],
) -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=37))
        for event in ending:
            bus.publish(event)
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the run ends and the spinner stops",
        )

        activity = app.query_one(ActivityStrip)
        assert app.query_one(ElapsedTimer).running is False
        assert activity.display is True
        assert activity.summary.display is True
        assert activity.query_one("#activity-live").display is False
        assert re.fullmatch(
            r"\* Worked for \d+(m\d\d)?s - done \d\d:\d\d", str(activity.summary.render())
        )


async def test_status_row_reflows_as_the_readouts_grow_wider() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=1, completion_tokens=58))
        bus.publish(GenerationCompleted(prompt_tokens=1, completion_tokens=91))

        counter = app.query_one(TokenCounter)
        await settle(
            pilot,
            lambda: (
                counter.render().plain == "149 tokens" and counter.size.width == len("149 tokens")
            ),
            "the counter widens to fit the reconciled total",
        )


async def test_first_pane_keeps_spinner_elapsed_and_tokens_running() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        await settle(pilot, lambda: len(app.query(AssistantPane)) == 1, "the first pane mounts")

        assert app.query_one(WaitingIndicator).running is True
        assert app.query_one(ElapsedTimer).running is True

        bus.publish(AssistantTextDelta(id="0", text="x" * 400))
        await settle(
            pilot,
            lambda: app.query_one(TokenCounter).render().plain == "~100 tokens",
            "the delta reaches the counter",
        )

        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: app.query_one(ElapsedTimer).running is False,
            "the elapsed timer stops with the run",
        )
        assert app.query_one(WaitingIndicator).running is False


async def test_stats_strip_sits_under_the_input_inside_the_footer() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        footer = app.query_one("#footer", Vertical)
        strip = app.query_one(StatsStrip)
        input_bar = app.query_one(InputBar)
        assert strip.parent is footer
        assert footer.children.index(input_bar) < footer.children.index(strip)
        assert len(app.query_one("#conversation", VerticalScroll).query(StatsStrip)) == 0


async def test_activity_strip_holds_spinner_tokens_and_timer_inside_the_conversation() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        strip = app.query_one(StatsStrip)
        footer_readouts = [
            type(child)
            for child in strip.children
            if isinstance(
                child,
                ContextMeter | RequestCounter | WaitingIndicator | TokenCounter | ElapsedTimer,
            )
        ]
        assert footer_readouts == [ContextMeter, RequestCounter]

        activity = app.query_one(ActivityStrip)
        conversation = app.query_one("#conversation", VerticalScroll)
        assert activity.parent is conversation
        activity_readouts = [
            type(child)
            for child in activity.query_one("#activity-live").children
            if isinstance(child, WaitingIndicator | TokenCounter | ElapsedTimer)
        ]
        assert activity_readouts == [WaitingIndicator, TokenCounter, ElapsedTimer]


async def test_activity_strip_stays_under_the_most_recent_pane() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        await settle(pilot, lambda: len(app.query(AssistantPane)) == 1, "the pane mounts")

        conversation = app.query_one("#conversation", VerticalScroll)
        assert isinstance(conversation.children[-1], ActivityStrip)
        assert conversation.children[-2] is app.query_one(AssistantPane)


async def test_spinner_keeps_running_through_thinking_text_and_tool_call_panes() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        bus.publish(AssistantThinkingDelta(id="0", text="pondering"))
        bus.publish(AssistantThinkingFinished(id="0"))
        await settle(pilot, lambda: len(app.query(ThinkingPane)) == 1, "the thinking pane mounts")
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(AssistantTextStarted(id="1"))
        bus.publish(AssistantTextDelta(id="1", text="answer"))
        bus.publish(AssistantTextFinished(id="1"))
        await settle(pilot, lambda: len(app.query(AssistantPane)) == 1, "the text pane mounts")
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(ToolCallStarted(id="2", name="search", arguments={}))
        await settle(pilot, lambda: len(app.query(ToolCallPane)) == 1, "the tool call pane mounts")
        assert app.query_one(WaitingIndicator).running is True

        tool_call = ToolCall(id="2", name="search", arguments={})
        bus.publish(ToolCallFinished(id="2", tool_call=tool_call, result="ok", is_error=False))
        await settle(pilot, lambda: app.query_one(ToolCallPane).finished, "the tool call finishes")
        assert app.query_one(WaitingIndicator).running is True
        assert app.query_one(ActivityStrip).display is True


async def test_spinner_stops_on_run_cancelled_after_panes() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="partial"))
        bus.publish(RunCancelled())
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the cancellation stops the spinner",
        )


async def test_spinner_stops_on_error_after_panes() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(ErrorOccurred(message="boom"))
        bus.publish(RunFinished(error="boom"))
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the error stops the spinner",
        )


async def test_elapsed_and_tokens_keep_updating_across_the_whole_turn() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        counter = app.query_one(TokenCounter)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="x" * 40))
        await settle(pilot, lambda: counter.tokens == 10, "the first delta is counted")

        bus.publish(ToolCallStarted(id="1", name="search", arguments={}))
        tool_call = ToolCall(id="1", name="search", arguments={})
        bus.publish(ToolCallFinished(id="1", tool_call=tool_call, result="ok", is_error=False))
        bus.publish(AssistantTextDelta(id="0", text="y" * 40))
        await settle(pilot, lambda: counter.tokens == 20, "counting survives a tool call")

        assert app.query_one(ElapsedTimer).running is True


async def test_spinner_restarts_and_elapsed_resets_on_a_queued_second_turn() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="first"))
        bus.publish(AssistantTextFinished(id="0"))
        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the first turn ends",
        )
        assert app.query_one(ElapsedTimer).running is False

        await submit(app, pilot, "b")
        bus.publish(RunStarted())
        await settle(
            pilot,
            lambda: app.query_one(ElapsedTimer).running is True,
            "the second turn restarts the timers",
        )

        assert app.query_one(WaitingIndicator).running is True
        assert app.query_one(ElapsedTimer).elapsed == 0


async def test_stats_strip_meter_and_requests_follow_generations() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue(), context_size=1000)
    async with app.run_test() as pilot:
        await pilot.pause()
        strip = app.query_one(StatsStrip)
        assert strip.meter.used == 0
        assert strip.requests.requests == 0

        await submit(app, pilot)
        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=10))
        await settle(pilot, lambda: strip.meter.used == 250, "the meter follows the prompt size")
        assert strip.requests.requests == 1

        bus.publish(GenerationCompleted(prompt_tokens=400, completion_tokens=10))
        bus.publish(RunFinished())
        await settle(pilot, lambda: strip.meter.used == 400, "the meter follows the second call")
        assert strip.requests.requests == 2
        assert strip.meter.ratio == 0.4


async def test_stats_strip_meter_survives_a_generation_without_prompt_tokens() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue(), context_size=1000)
    async with app.run_test() as pilot:
        await pilot.pause()
        strip = app.query_one(StatsStrip)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=10))
        await settle(pilot, lambda: strip.meter.used == 250, "the meter records the prompt size")

        bus.publish(GenerationCompleted(prompt_tokens=None, completion_tokens=None))
        await settle(pilot, lambda: strip.requests.requests == 2, "the request still counts")
        assert strip.meter.used == 250


async def test_new_session_resets_every_stat() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle, context_size=1000)
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=37))
        bus.publish(RunFinished())
        strip = app.query_one(StatsStrip)
        await settle(pilot, lambda: strip.meter.used == 250, "the first session accumulates stats")

        await pilot.press("ctrl+n")
        await settle(pilot, lambda: strip.meter.used == 0, "the meter empties")

        assert strip.requests.requests == 0
        activity = app.query_one(ActivityStrip)
        assert activity.counter.render().plain == "~0 tokens"
        assert activity.timer.elapsed == 0
        assert activity.timer.running is False
        assert activity.indicator.running is False
        assert activity.display is False


async def test_stats_strip_uses_the_configured_context_size() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue(), context_size=32000)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert "0/32k" in app.query_one(StatsStrip).meter.render().plain


async def test_request_counter_shows_progress_against_a_known_generation_budget() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue(), context_size=1000, max_steps=50)
    async with app.run_test() as pilot:
        await pilot.pause()
        strip = app.query_one(StatsStrip)
        assert strip.requests.render().plain == "0/50 req"

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=100, completion_tokens=5, iteration=3))
        await settle(pilot, lambda: strip.requests.requests == 3, "the counter follows the run")

        assert strip.requests.render().plain == "3/50 req"


async def test_request_counter_without_a_budget_keeps_the_plain_readout() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue(), context_size=1000)
    async with app.run_test() as pilot:
        await pilot.pause()
        strip = app.query_one(StatsStrip)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=100, completion_tokens=5, iteration=3))
        await settle(pilot, lambda: strip.requests.requests == 3, "the counter follows the run")

        assert strip.requests.render().plain == "3 req"


async def test_meter_band_follows_pressure_and_dying_across_generations() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue(), context_size=1000, max_steps=3)
    async with app.run_test() as pilot:
        await pilot.pause()
        meter = app.query_one(StatsStrip).meter

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=100, iteration=2, pressure=True))
        await settle(pilot, lambda: meter.pressure, "the warning band raises under pressure")
        assert meter.dying is False

        bus.publish(GenerationCompleted(prompt_tokens=100, iteration=4))
        await settle(pilot, lambda: meter.dying, "the last-words generation raises the error band")
        assert meter.pressure is False

        bus.publish(RunFinished())
