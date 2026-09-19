import queue

from pico.core.bus import Bus
from pico.core.events import RunFinished, RunStarted
from pico.tui.app import ChatInput, PicoApp
from pico.tui.messages import UserInputSubmitted
from pico.tui.widgets import UserPane, WaitingIndicator
from tests.conftest import settle
from tests.tui.app_fixtures import submit


async def test_input_bar_submission_emits_message_and_clears_field() -> None:
    bus = Bus()
    submitted: list[str] = []

    class TrackingApp(PicoApp):
        async def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
            submitted.append(message.text)

    app = TrackingApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hi")
        await pilot.press("enter")
        await settle(pilot, lambda: submitted == ["hi"], "the submission is delivered")

        assert app.query_one("#user-input", ChatInput).text == ""


async def test_ctrl_j_inserts_newline_without_submitting() -> None:
    bus = Bus()
    submitted: list[str] = []

    class TrackingApp(PicoApp):
        async def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
            submitted.append(message.text)

    app = TrackingApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"ab")
        await pilot.press("ctrl+j")
        await pilot.press(*"cd")
        await pilot.pause()

        assert submitted == []
        assert app.query_one("#user-input", ChatInput).text == "ab\ncd"


async def test_clicking_outside_input_keeps_focus_on_input() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        assert text_input.has_focus is True

        await pilot.click("#conversation")
        await pilot.pause()

        assert text_input.has_focus is True

        await pilot.press(*"xy")
        await pilot.pause()
        assert text_input.text == "xy"


async def test_submitting_input_mounts_user_pane_and_enqueues_text() -> None:
    bus = Bus()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(bus, input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot, "hi")

        await settle(pilot, lambda: len(app.query(UserPane)) == 1, "the user pane mounts")
        assert app.query_one(UserPane).render().plain == "hi"
        assert input_queue.get_nowait() == "hi"


async def test_initial_prompt_is_submitted_on_mount() -> None:
    bus = Bus()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(bus, input_queue, initial_prompt="do x and y")
    async with app.run_test() as pilot:
        await settle(pilot, lambda: len(app.query(UserPane)) == 1, "the user pane mounts")
        assert app.query_one(UserPane).render().plain == "do x and y"
        assert input_queue.get_nowait() == "do x and y"


async def test_without_initial_prompt_nothing_is_submitted() -> None:
    bus = Bus()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(bus, input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert len(app.query(UserPane)) == 0
        assert input_queue.empty()


async def test_first_message_is_not_marked_queued() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot)

        await settle(pilot, lambda: len(app.query(UserPane)) == 1, "the user pane mounts")
        assert app.query_one(UserPane).queued is False


async def test_message_sent_while_run_in_flight_is_marked_queued() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot, "a")
        bus.publish(RunStarted())
        await settle(
            pilot, lambda: app.query_one(WaitingIndicator).running is True, "the run starts"
        )

        await submit(app, pilot, "b")
        await settle(
            pilot,
            lambda: [pane.queued for pane in app.query(UserPane)] == [False, True],
            "the second message is queued behind the first",
        )


async def test_second_message_marked_queued_even_before_run_started_arrives() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot, "a")
        await submit(app, pilot, "b")

        await settle(
            pilot,
            lambda: [pane.queued for pane in app.query(UserPane)] == [False, True],
            "the second message is queued without a run having started",
        )


async def test_run_started_unmarks_the_oldest_queued_pane() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot, "a")
        await submit(app, pilot, "b")
        await settle(
            pilot,
            lambda: [pane.queued for pane in app.query(UserPane)] == [False, True],
            "the second message starts queued",
        )

        bus.publish(RunStarted())
        bus.publish(RunFinished())
        bus.publish(RunStarted())

        await settle(
            pilot,
            lambda: [pane.queued for pane in app.query(UserPane)] == [False, False],
            "the second turn unmarks the queued pane",
        )


async def test_submitting_blank_input_does_not_enqueue() -> None:
    bus = Bus()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(bus, input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert input_queue.empty()
