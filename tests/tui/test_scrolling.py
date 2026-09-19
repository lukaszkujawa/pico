import queue

from textual.containers import VerticalScroll
from textual.pilot import Pilot

from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    RunFinished,
    RunStarted,
)
from pico.tui.app import InputBar, PicoApp
from pico.tui.widgets import AssistantPane, Splash
from tests.conftest import settle


async def test_conversation_scroll_offset_is_never_negative_on_startup() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        conversation = app.query_one("#conversation", VerticalScroll)
        assert conversation.scroll_offset.y >= 0


async def _stream_overflowing_panes(bus: Bus, app: PicoApp, pilot: Pilot[None], count: int) -> None:
    for i in range(count):
        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id=str(i)))
        bus.publish(AssistantTextDelta(id=str(i), text="line " * 20))
        bus.publish(AssistantTextFinished(id=str(i)))
        bus.publish(RunFinished())
    await settle(
        pilot,
        lambda: len(app.query(AssistantPane)) == count,
        f"all {count} assistant panes mount",
    )


async def test_conversation_stays_pinned_to_bottom_as_panes_overflow_the_viewport() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        conversation = app.query_one("#conversation", VerticalScroll)
        await _stream_overflowing_panes(bus, app, pilot, 30)

        last_pane = app.query(AssistantPane)[-1]
        await settle(
            pilot,
            lambda: (
                last_pane.region.y < conversation.region.bottom
                and last_pane.region.bottom > conversation.region.y
            ),
            "the newest pane is inside the viewport",
        )
        assert conversation.scroll_offset.y >= 0


async def test_manual_scroll_up_is_not_overridden_by_the_next_delta() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        conversation = app.query_one("#conversation", VerticalScroll)
        await _stream_overflowing_panes(bus, app, pilot, 30)

        conversation.scroll_home(animate=False, immediate=True)
        await settle(
            pilot,
            lambda: conversation.scroll_offset.y < conversation.max_scroll_y,
            "the conversation is scrolled away from the bottom",
        )
        scrolled_up_offset = conversation.scroll_offset.y

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="new"))
        bus.publish(AssistantTextDelta(id="new", text="more text"))
        bus.publish(AssistantTextFinished(id="new"))
        bus.publish(RunFinished())
        await settle(
            pilot, lambda: len(app.query(AssistantPane)) == 31, "the new pane mounts below"
        )

        assert conversation.scroll_offset.y == scrolled_up_offset


async def test_splash_renders_on_startup() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(Splash)) == 1


async def test_layout_renders_at_small_and_large_terminal_sizes() -> None:
    for size in [(80, 24), (160, 48)]:
        bus = Bus()
        app = PicoApp(bus, queue.Queue())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            input_bar = app.query_one(InputBar)
            assert input_bar.region.width == size[0]
