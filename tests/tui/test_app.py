import queue

from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.app import ChatInput, InputBar, PicoApp
from pico.tui.messages import UserInputSubmitted
from pico.tui.widgets import (
    AssistantPane,
    ErrorPane,
    Splash,
    ThinkingPane,
    ToolCallPane,
    UserPane,
    WaitingIndicator,
)


async def test_app_renders_assistant_pane_from_bus_events() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
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
    app = PicoApp(bus, queue.Queue())
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
        assert "found it" in pane.render().plain
        assert pane.finished is True
        assert pane.is_error is False
        assert pane.fact_index == 0


async def test_fact_index_increments_across_successful_tool_calls_only() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        tool_call = ToolCall(id="1", name="search", arguments={})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search"))
        bus.publish(ToolCallFinished(id="1", tool_call=tool_call, result="first", is_error=False))
        bus.publish(ToolCallStarted(id="2", name="search"))
        bus.publish(ToolCallFinished(id="2", tool_call=tool_call, result="oops", is_error=True))
        bus.publish(ToolCallStarted(id="3", name="search"))
        bus.publish(ToolCallFinished(id="3", tool_call=tool_call, result="second", is_error=False))
        bus.publish(RunFinished())

        await pilot.pause(0.2)

        panes = app.query(ToolCallPane)
        assert len(panes) == 3
        assert panes[0].fact_index == 0
        assert panes[1].fact_index is None
        assert panes[2].fact_index == 1


async def test_thinking_then_text_produces_one_pane_each() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        bus.publish(AssistantThinkingDelta(id="0", text="pondering"))
        bus.publish(AssistantThinkingFinished(id="0"))
        bus.publish(AssistantTextStarted(id="1"))
        bus.publish(AssistantTextDelta(id="1", text="answer"))
        bus.publish(AssistantTextFinished(id="1"))
        bus.publish(RunFinished())

        await pilot.pause(0.2)

        thinking_panes = app.query(ThinkingPane)
        assistant_panes = app.query(AssistantPane)
        assert len(thinking_panes) == 1
        assert len(assistant_panes) == 1
        assert thinking_panes.first().finished is True
        assert assistant_panes.first().finished is True


async def test_second_turn_creates_new_panes_not_reused() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        bus.publish(AssistantThinkingDelta(id="0", text="first thought"))
        bus.publish(AssistantThinkingFinished(id="0"))
        bus.publish(AssistantTextStarted(id="1"))
        bus.publish(AssistantTextDelta(id="1", text="first answer"))
        bus.publish(AssistantTextFinished(id="1"))
        bus.publish(RunFinished())
        await pilot.pause(0.2)

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="2"))
        bus.publish(AssistantThinkingDelta(id="2", text="second thought"))
        bus.publish(AssistantThinkingFinished(id="2"))
        bus.publish(AssistantTextStarted(id="3"))
        bus.publish(AssistantTextDelta(id="3", text="second answer"))
        bus.publish(AssistantTextFinished(id="3"))
        bus.publish(RunFinished())
        await pilot.pause(0.2)

        thinking_panes = app.query(ThinkingPane)
        assistant_panes = app.query(AssistantPane)
        assert len(thinking_panes) == 2
        assert len(assistant_panes) == 2
        assert {pane.render().plain for pane in thinking_panes} == {
            "first thought",
            "second thought",
        }
        assert {pane.render().plain for pane in assistant_panes} == {
            "first answer",
            "second answer",
        }


async def test_app_handles_multiple_panes_and_error() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
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


async def test_input_bar_submission_emits_message_and_clears_field() -> None:
    bus = Bus()
    submitted: list[str] = []

    class TrackingApp(PicoApp):
        def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
            submitted.append(message.text)

    app = TrackingApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello pico")
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert submitted == ["hello pico"]
        assert app.query_one("#user-input", ChatInput).text == ""


async def test_ctrl_j_inserts_newline_without_submitting() -> None:
    bus = Bus()
    submitted: list[str] = []

    class TrackingApp(PicoApp):
        def on_user_input_submitted(self, message: UserInputSubmitted) -> None:
            submitted.append(message.text)

    app = TrackingApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"line one")
        await pilot.press("ctrl+j")
        await pilot.press(*"line two")
        await pilot.pause()

        assert submitted == []
        assert app.query_one("#user-input", ChatInput).text == "line one\nline two"


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

        await pilot.press(*"still typing")
        await pilot.pause()
        assert text_input.text == "still typing"


async def test_submitting_input_mounts_user_pane_immediately() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello pico")
        await pilot.press("enter")
        await pilot.pause()

        panes = app.query(UserPane)
        assert len(panes) == 1
        assert panes.first().render().plain == "hello pico"


async def test_submitting_input_enqueues_text() -> None:
    bus = Bus()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(bus, input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello pico")
        await pilot.press("enter")
        await pilot.pause()

        assert input_queue.get_nowait() == "hello pico"


async def test_first_message_is_not_marked_queued() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

        pane = app.query_one(UserPane)
        assert pane.queued is False


async def test_message_sent_while_run_in_flight_is_marked_queued() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"first")
        await pilot.press("enter")
        await pilot.pause()
        bus.publish(RunStarted())
        await pilot.pause(0.2)

        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()

        panes = app.query(UserPane)
        assert panes[0].queued is False
        assert panes[1].queued is True


async def test_second_message_marked_queued_even_before_run_started_arrives() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"first")
        await pilot.press("enter")
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()

        panes = app.query(UserPane)
        assert panes[0].queued is False
        assert panes[1].queued is True


async def test_run_started_unmarks_the_oldest_queued_pane() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"first")
        await pilot.press("enter")
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"second")
        await pilot.press("enter")
        await pilot.pause()

        bus.publish(RunStarted())
        await pilot.pause(0.2)
        bus.publish(RunFinished())
        await pilot.pause(0.2)

        bus.publish(RunStarted())
        await pilot.pause(0.2)

        panes = app.query(UserPane)
        assert panes[0].queued is False
        assert panes[1].queued is False


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


async def test_submitting_input_starts_waiting_indicator() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()

        assert app.query_one(WaitingIndicator).running is True


async def test_first_pane_create_stops_waiting_indicator() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        await pilot.pause(0.2)

        assert app.query_one(WaitingIndicator).running is False


async def test_error_before_any_pane_stops_waiting_indicator() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(ErrorOccurred(message="boom"))
        bus.publish(RunFinished(error="boom"))
        await pilot.pause(0.2)

        assert app.query_one(WaitingIndicator).running is False


class RecordingCancelHandle:
    def __init__(self) -> None:
        self.trigger_count = 0

    def trigger(self) -> None:
        self.trigger_count += 1


async def test_escape_during_turn_triggers_cancel_handle_once() -> None:
    bus = Bus()
    cancel_handle = RecordingCancelHandle()
    app = PicoApp(bus, queue.Queue(), cancel_handle)
    async with app.run_test() as pilot:
        await pilot.pause()
        bus.publish(RunStarted())
        await pilot.pause(0.2)

        await pilot.press("escape")
        await pilot.pause()

        assert cancel_handle.trigger_count == 1


async def test_escape_while_idle_does_nothing() -> None:
    bus = Bus()
    cancel_handle = RecordingCancelHandle()
    app = PicoApp(bus, queue.Queue(), cancel_handle)
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("escape")
        await pilot.pause()

        assert cancel_handle.trigger_count == 0


async def test_run_cancelled_stops_waiting_indicator() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(RunCancelled())
        await pilot.pause(0.2)

        assert app.query_one(WaitingIndicator).running is False
