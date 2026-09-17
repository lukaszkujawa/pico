import queue

import pytest
from textual.containers import VerticalScroll
from textual.pilot import Pilot

from pico.core.bus import Bus
from pico.core.events import (
    AnswerSettled,
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
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.app import ChatInput, InputBar, PicoApp
from pico.tui.messages import UserInputSubmitted
from pico.tui.widgets import (
    AnswerPane,
    AssistantPane,
    ElapsedTimer,
    ErrorPane,
    Splash,
    StatusLine,
    ThinkingPane,
    TokenCounter,
    ToolCallPane,
    UserPane,
    WaitingIndicator,
)
from tests.conftest import settle


async def _submit(app: PicoApp, pilot: Pilot[None], text: str = "hi") -> None:
    app.query_one("#user-input", ChatInput).focus()
    await pilot.pause()
    await pilot.press(*text)
    await pilot.press("enter")
    await settle(pilot, lambda: app.query_one("#user-input", ChatInput).text == "", "input clears")


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

        await settle(
            pilot,
            lambda: [pane.render().plain for pane in app.query(AssistantPane)] == ["Hello, world!"],
            "the assistant pane shows the joined deltas",
        )


async def test_app_renders_tool_call_pane_from_bus_events() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        tool_call = ToolCall(id="1", name="search", arguments={"q": "pico"})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search", arguments={"q": "pico"}))
        bus.publish(
            ToolCallFinished(
                id="1", tool_call=tool_call, result="found it", is_error=False, fact_id=2
            )
        )
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: len(app.query(ToolCallPane)) == 1 and app.query_one(ToolCallPane).finished,
            "the tool call pane finishes",
        )

        pane = app.query_one(ToolCallPane)
        assert pane.name_label == "search"
        assert "pico" in pane.render().plain
        assert "found it" in pane.render().plain
        assert pane.is_error is False
        assert pane.fact_index == 2


async def test_tool_call_pane_created_lazily_when_delta_arrives_before_started() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        tool_call = ToolCall(id="1", name="search", arguments={"q": "pico"})
        bus.publish(RunStarted())
        bus.publish(ToolCallArgumentsDelta(id="1", name="search", text='{"q": "pico"}'))
        bus.publish(ToolCallStarted(id="1", name="search", arguments={"q": "pico"}))
        bus.publish(
            ToolCallFinished(
                id="1", tool_call=tool_call, result="found it", is_error=False, fact_id=2
            )
        )
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: len(app.query(ToolCallPane)) == 1 and app.query_one(ToolCallPane).finished,
            "the lazily created tool call pane finishes",
        )

        pane = app.query_one(ToolCallPane)
        assert pane.name_label == "search"
        assert "pico" in pane.render().plain
        assert "found it" in pane.render().plain


async def test_two_tool_calls_with_distinct_pane_ids_both_mount_without_crashing() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        first_call = ToolCall(id="0", name="search", arguments={"q": "first"})
        second_call = ToolCall(id="0", name="search", arguments={"q": "second"})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="0", name="search", arguments={"q": "first"}))
        bus.publish(ToolCallFinished(id="0", tool_call=first_call, result="one", is_error=False))
        bus.publish(ToolCallStarted(id="1", name="search", arguments={"q": "second"}))
        bus.publish(ToolCallFinished(id="1", tool_call=second_call, result="two", is_error=False))
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: (
                {pane.finished for pane in app.query(ToolCallPane)} == {True}
                and len(app.query(ToolCallPane)) == 2
            ),
            "both tool call panes finish",
        )

        panes = app.query(ToolCallPane)
        assert {pane.render().plain.count("one") for pane in panes} != {0}


async def test_second_session_tool_call_panes_do_not_collide_with_first_sessions_ids() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()

        first_call = ToolCall(id="0", name="search", arguments={})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="0", name="search", arguments={}))
        bus.publish(ToolCallFinished(id="0", tool_call=first_call, result="one", is_error=False))
        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: [pane.finished for pane in app.query(ToolCallPane)] == [True],
            "the first session's tool call pane finishes",
        )

        await pilot.press("ctrl+n")
        await settle(
            pilot, lambda: len(app.query(ToolCallPane)) == 0, "the conversation is cleared"
        )

        second_call = ToolCall(id="0", name="search", arguments={})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="0", name="search", arguments={}))
        bus.publish(ToolCallFinished(id="0", tool_call=second_call, result="two", is_error=False))
        bus.publish(RunFinished())
        await settle(
            pilot,
            lambda: [pane.finished for pane in app.query(ToolCallPane)] == [True],
            "the second session's tool call pane finishes",
        )

        assert "two" in app.query_one(ToolCallPane).render().plain


async def test_fact_index_reflects_real_non_contiguous_fact_ids() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        tool_call = ToolCall(id="1", name="search", arguments={})
        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="search", arguments={}))
        bus.publish(
            ToolCallFinished(id="1", tool_call=tool_call, result="first", is_error=False, fact_id=2)
        )
        bus.publish(ToolCallStarted(id="2", name="search", arguments={}))
        bus.publish(
            ToolCallFinished(
                id="2", tool_call=tool_call, result="oops", is_error=True, fact_id=None
            )
        )
        bus.publish(ToolCallStarted(id="3", name="search", arguments={}))
        bus.publish(
            ToolCallFinished(
                id="3", tool_call=tool_call, result="second", is_error=False, fact_id=7
            )
        )
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: [pane.fact_index for pane in app.query(ToolCallPane)] == [2, None, 7],
            "all three panes carry their own fact id",
        )


async def test_answer_call_mounts_answer_pane_from_the_moment_it_starts() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(
            ToolCallStarted(id="1", name="answer", arguments={"content": "42", "citations": []})
        )

        await settle(pilot, lambda: len(app.query(AnswerPane)) == 1, "the answer pane mounts")
        assert len(app.query(ToolCallPane)) == 0

        bus.publish(AnswerSettled(id="1", content="42", accepted=True, reason=None, verify=None))
        bus.publish(RunFinished())
        await settle(pilot, lambda: app.query_one(AnswerPane).settled, "the answer settles")

        assert len(app.query(AnswerPane)) == 1
        assert len(app.query(ToolCallPane)) == 0
        assert "42" in app.query_one(AnswerPane).render().plain


async def test_rejected_answer_leaves_pane_mounted_showing_reason() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="answer", arguments={}))
        bus.publish(
            AnswerSettled(
                id="1",
                content="",
                accepted=False,
                reason="unknown fact citation(s): [3]",
                verify=None,
            )
        )
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: len(app.query(AnswerPane)) == 1 and app.query_one(AnswerPane).settled,
            "the rejected answer settles",
        )

        pane = app.query_one(AnswerPane)
        assert len(app.query(ToolCallPane)) == 0
        assert pane.accepted is False
        assert "unknown fact citation(s): [3]" in pane.render().plain


async def test_no_raw_json_appears_for_an_answer_call() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(
            ToolCallArgumentsDelta(
                id="1",
                name="answer",
                text='{"content": "There are 17 dirs", "citations": []}',
            )
        )
        bus.publish(
            ToolCallStarted(
                id="1", name="answer", arguments={"content": "There are 17 dirs", "citations": []}
            )
        )
        bus.publish(
            AnswerSettled(
                id="1", content="There are 17 dirs", accepted=True, reason=None, verify=None
            )
        )
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: len(app.query(AnswerPane)) == 1 and app.query_one(AnswerPane).settled,
            "the answer settles",
        )

        assert '{"content"' not in app.query_one(AnswerPane).render().plain


async def test_rejected_then_accepted_answer_leaves_two_answer_panes_in_order() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="answer", arguments={}))
        bus.publish(
            AnswerSettled(
                id="1", content="", accepted=False, reason="unknown fact citation(s): [3]"
            )
        )
        bus.publish(ToolCallStarted(id="2", name="answer", arguments={"content": "42"}))
        bus.publish(AnswerSettled(id="2", content="42", accepted=True, reason=None, verify=None))
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: [pane.accepted for pane in app.query(AnswerPane)] == [False, True],
            "both answer panes settle in order",
        )


async def test_long_streamed_thinking_text_wraps_taller_than_one_line() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="0"))
        bus.publish(AssistantThinkingDelta(id="0", text="word " * 100))

        await settle(
            pilot,
            lambda: (
                len(app.query(ThinkingPane)) == 1 and app.query_one(ThinkingPane).size.height > 1
            ),
            "the thinking pane wraps onto several lines",
        )


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

        await settle(
            pilot,
            lambda: len(app.query(ThinkingPane)) == 1 and len(app.query(AssistantPane)) == 1,
            "one thinking pane and one assistant pane exist",
        )


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

        bus.publish(RunStarted())
        bus.publish(AssistantThinkingStarted(id="2"))
        bus.publish(AssistantThinkingDelta(id="2", text="second thought"))
        bus.publish(AssistantThinkingFinished(id="2"))
        bus.publish(AssistantTextStarted(id="3"))
        bus.publish(AssistantTextDelta(id="3", text="second answer"))
        bus.publish(AssistantTextFinished(id="3"))
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: (
                {pane.render().plain for pane in app.query(ThinkingPane)}
                == {"first thought", "second thought"}
                and {pane.render().plain for pane in app.query(AssistantPane)}
                == {"first answer", "second answer"}
            ),
            "each turn has its own pair of panes",
        )


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
        bus.publish(ToolCallStarted(id="1", name="search", arguments={}))
        bus.publish(ToolCallFinished(id="1", tool_call=tool_call, result="oops", is_error=True))
        bus.publish(ErrorOccurred(message="something broke"))
        bus.publish(RunFinished(error="something broke"))

        await settle(pilot, lambda: len(app.query(ErrorPane)) == 1, "the error pane mounts")

        assert len(app.query(AssistantPane)) == 1
        assert [pane.is_error for pane in app.query(ToolCallPane)] == [True]


async def test_run_finished_with_error_and_no_error_occurred_shows_error() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(RunFinished(error="boom"))

        await settle(pilot, lambda: len(app.query(ErrorPane)) == 1, "the error pane mounts")
        assert "boom" in app.query_one(ErrorPane).render().plain


async def test_run_finished_without_error_shows_no_error_pane() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AssistantTextStarted(id="0"))
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the run finishes",
        )
        assert len(app.query(ErrorPane)) == 0


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


async def test_bus_consumer_exception_surfaces_as_error_pane_not_a_dead_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pico.tui.app as app_module

    def raising_translate(event: object) -> object:
        raise ValueError("boom")

    monkeypatch.setattr(app_module, "translate", raising_translate)

    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        await settle(pilot, lambda: len(app.query(ErrorPane)) == 1, "the error pane mounts")


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
        await _submit(app, pilot, "hi")

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
        await _submit(app, pilot)

        await settle(pilot, lambda: len(app.query(UserPane)) == 1, "the user pane mounts")
        assert app.query_one(UserPane).queued is False


async def test_message_sent_while_run_in_flight_is_marked_queued() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot, "a")
        bus.publish(RunStarted())
        await settle(
            pilot, lambda: app.query_one(WaitingIndicator).running is True, "the run starts"
        )

        await _submit(app, pilot, "b")
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
        await _submit(app, pilot, "a")
        await _submit(app, pilot, "b")

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
        await _submit(app, pilot, "a")
        await _submit(app, pilot, "b")
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


async def test_first_pane_create_keeps_waiting_indicator_running() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot)
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
        await _submit(app, pilot)
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(ErrorOccurred(message="boom"))
        bus.publish(RunFinished(error="boom"))
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the waiting indicator stops",
        )


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
        await _submit(app, pilot)
        assert app.query_one(WaitingIndicator).running is True

        bus.publish(RunStarted())
        bus.publish(RunCancelled())
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the cancelled run stops the waiting indicator",
        )


async def test_streaming_deltas_climb_the_token_estimate() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot)
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
        await _submit(app, pilot)
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
        await _submit(app, pilot)
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
        await _submit(app, pilot)
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
        await _submit(app, pilot)
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
        status = app.query_one(StatusLine)
        assert status.display is False

        await _submit(app, pilot)

        assert status.display is True
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
async def test_run_ending_freezes_elapsed_and_tokens_while_stopping_spinner(
    ending: list[BusEvent],
) -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=250, completion_tokens=37))
        for event in ending:
            bus.publish(event)
        await settle(
            pilot,
            lambda: app.query_one(WaitingIndicator).running is False,
            "the run ends and the spinner stops",
        )

        assert app.query_one(ElapsedTimer).running is False
        assert app.query_one(StatusLine).display is True
        assert app.query_one(TokenCounter).render().plain == "37 tokens"


async def test_status_row_reflows_as_the_readouts_grow_wider() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test(size=(64, 22)) as pilot:
        await pilot.pause()
        await _submit(app, pilot)

        bus.publish(RunStarted())
        bus.publish(GenerationCompleted(prompt_tokens=1, completion_tokens=58))
        bus.publish(GenerationCompleted(prompt_tokens=1, completion_tokens=91))
        bus.publish(RunFinished())

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
        await _submit(app, pilot)

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


class RecordingSessionHandle:
    def __init__(self, session_id: str = "session-1") -> None:
        self._session_id = session_id
        self.start_count = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    def start_new(self) -> None:
        self.start_count += 1
        self._session_id = f"session-{self.start_count + 1}"


async def test_new_session_action_clears_conversation_and_starts_a_session() -> None:
    bus = Bus()
    session_handle = RecordingSessionHandle()
    app = PicoApp(bus, queue.Queue(), None, session_handle)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot)

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
        assert len(app.query(StatusLine)) == 1


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


async def test_status_line_stays_in_conversation_as_its_last_child() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        status = app.query_one(StatusLine)
        conversation = app.query_one("#conversation", VerticalScroll)
        assert status.parent is conversation
        assert conversation.children[-1] is status


async def test_spinner_keeps_running_through_thinking_text_and_tool_call_panes() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot)

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
        assert app.query_one(StatusLine).display is True


async def test_spinner_stops_on_run_cancelled_after_panes() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        await _submit(app, pilot)

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
        await _submit(app, pilot)

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
        await _submit(app, pilot)
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
        await _submit(app, pilot)

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

        await _submit(app, pilot, "b")
        bus.publish(RunStarted())
        await settle(
            pilot,
            lambda: app.query_one(ElapsedTimer).running is True,
            "the second turn restarts the timers",
        )

        assert app.query_one(WaitingIndicator).running is True
        assert app.query_one(ElapsedTimer).elapsed == 0
