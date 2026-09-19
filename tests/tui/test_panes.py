import queue

import pytest

from pico.core.bus import Bus
from pico.core.events import (
    AnswerSettled,
    AssistantTextDelta,
    AssistantTextFinished,
    AssistantTextStarted,
    AssistantThinkingDelta,
    AssistantThinkingFinished,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunFinished,
    RunStarted,
    ToolCallArgumentsDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from pico.llm.types import ToolCall
from pico.tui.app import PicoApp
from pico.tui.widgets import (
    SUCCESS_GLYPH,
    AnswerPane,
    AssistantPane,
    ErrorPane,
    ThinkingPane,
    ToolCallPane,
    WaitingIndicator,
)
from tests.conftest import settle
from tests.tui.app_fixtures import RecordingSessionHandle


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


async def test_answer_pane_created_lazily_when_settle_arrives_without_started() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(AnswerSettled(id="24", content="degraded ending", accepted=True, reason=None))
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: len(app.query(AnswerPane)) == 1 and app.query_one(AnswerPane).settled,
            "the lazily created answer pane settles",
        )

        assert "degraded ending" in app.query_one(AnswerPane).render().plain


async def test_incomplete_answer_settles_without_the_success_glyph() -> None:
    bus = Bus()
    app = PicoApp(bus, queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()

        bus.publish(RunStarted())
        bus.publish(ToolCallStarted(id="1", name="answer", arguments={}))
        bus.publish(AnswerSettled(id="1", content="last narration", accepted=True, complete=False))
        bus.publish(RunFinished())

        await settle(
            pilot,
            lambda: len(app.query(AnswerPane)) == 1 and app.query_one(AnswerPane).settled,
            "the incomplete answer settles",
        )

        rendered = app.query_one(AnswerPane).render().plain
        assert "last narration" in rendered
        assert "unverified" not in rendered
        assert SUCCESS_GLYPH not in rendered


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
