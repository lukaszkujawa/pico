import queue

from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.app import PicoApp
from pico.tui.widgets import (
    AssistantPane,
    Splash,
    StatsStrip,
    ToolCallPane,
    UserPane,
    WaitingIndicator,
)
from tests.conftest import settle
from tests.tui.app_fixtures import RecordingCancelHandle, RecordingSessionHandle, submit


async def test_escape_during_turn_triggers_cancel_handle_once() -> None:
    bus = Bus()
    cancel_handle = RecordingCancelHandle()
    app = PicoApp(bus, queue.Queue(), cancel_handle)
    async with app.run_test() as pilot:
        await pilot.pause()
        bus.publish(RunStarted())
        await settle(
            pilot, lambda: app.query_one(WaitingIndicator).running is True, "the run starts"
        )

        await pilot.press("escape")
        await settle(pilot, lambda: cancel_handle.trigger_count == 1, "the cancel handle fires")


async def test_escape_while_idle_does_nothing() -> None:
    bus = Bus()
    cancel_handle = RecordingCancelHandle()
    app = PicoApp(bus, queue.Queue(), cancel_handle)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert cancel_handle.trigger_count == 0


async def test_run_cancelled_closes_open_tool_call_pane_and_stops_its_timer() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search", arguments={}))
        await settle(pilot, lambda: len(app.query(ToolCallPane)) == 1, "the tool call pane mounts")

        pane = app.query_one(ToolCallPane)
        assert pane.finished is False
        assert pane._timer is not None  # pyright: ignore[reportPrivateUsage]

        bus.publish(RunCancelled())
        await settle(pilot, lambda: pane.finished, "the cancelled pane finishes")

        assert pane._timer is None  # pyright: ignore[reportPrivateUsage]


async def test_new_session_closes_open_tool_call_pane_and_stops_its_timer() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search", arguments={}))
        await settle(pilot, lambda: len(app.query(ToolCallPane)) == 1, "the tool call pane mounts")

        pane = app.query_one(ToolCallPane)
        assert pane.finished is False

        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the run finishes so a new session is allowed",
        )
        await pilot.press("ctrl+n")
        await settle(pilot, lambda: pane.finished, "the pane is closed by the new session")

        assert pane._timer is None  # pyright: ignore[reportPrivateUsage]


async def test_run_cancelled_stops_waiting_indicator() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(RunCancelled())
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the cancelled run stops the waiting indicator",
        )


async def test_new_session_action_clears_conversation_and_starts_a_session() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(AssistantTextDelta(id="0", text="hi"))
        bus.publish(AssistantTextFinished(id="0"))
        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: len(app.query(UserPane)) == 1 and len(app.query(AssistantPane)) == 1,
            "the turn's panes are on screen",
        )

        await pilot.press("ctrl+n")
        await settle(
            pilot,
            lambda: len(app.query(UserPane)) == 0 and len(app.query(AssistantPane)) == 0,
            "the conversation is cleared",
        )

        assert session_handle.start_count == 1
        assert len(app.query(Splash)) == 1
        assert len(app.query(StatsStrip)) == 1


async def test_new_session_action_is_a_no_op_during_a_run() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()
        bus.publish(RunStarted())
        await settle(
            pilot, lambda: app.query_one(WaitingIndicator).running is True, "the run starts"
        )

        await pilot.press("ctrl+n")
        await pilot.pause()

        assert session_handle.start_count == 0


async def test_new_session_action_shows_fact_ids_from_new_session_events() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()
        tool_call = ToolCall(id="1", name="search", arguments={"q": "pico"})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search", arguments={"q": "pico"}))
        bus.publish(
            ToolCallFinished(id="1", tool_call=tool_call, result="ok", is_error=False, fact_id=1)
        )
        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: [pane.finished for pane in app.query(ToolCallPane)] == [True],
            "the first session's pane finishes",
        )

        await pilot.press("ctrl+n")
        await settle(
            pilot, lambda: len(app.query(ToolCallPane)) == 0, "the conversation is cleared"
        )

        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="2", name="search", arguments={"q": "pico"}))
        bus.publish(
            ToolCallFinished(
                id="2",
                tool_call=ToolCall(id="2", name="search", arguments={"q": "pico"}),
                result="ok",
                is_error=False,
                fact_id=1,
            )
        )
        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: [pane.fact_index for pane in app.query(ToolCallPane)] == [1],
            "the new session's pane shows its own fact id",
        )


async def test_splash_shows_active_session_id_and_updates_after_new_session() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert "session-1" in app.query_one(Splash).render().plain

        await pilot.press("ctrl+n")
        await settle(
            pilot,
            lambda: "session-2" in app.query_one(Splash).render().plain,
            "the splash shows the new session id",
        )


async def test_new_session_action_without_a_session_handle_is_a_no_op() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("ctrl+n")
        await pilot.pause()

        assert app.query_one(Splash).render().plain.count("session ") == 0
