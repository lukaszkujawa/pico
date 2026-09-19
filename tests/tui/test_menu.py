import queue

from pico.core.bus import Bus
from pico.tui.app import ChatInput, PicoApp
from pico.tui.commands import COMMANDS, Options, complete
from pico.tui.widgets import (
    CURRENT_GLYPH,
    FETCHING_LABEL,
    CommandMenu,
    SystemPane,
    UserPane,
)
from tests.conftest import settle
from tests.tui.app_fixtures import FakeSwitch, GatedSwitch, submit


async def test_quit_command_exits_without_enqueuing_anything() -> None:
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/quit", "enter")
        await settle(pilot, lambda: not app.is_running, "the app exits")

    assert input_queue.empty()
    assert len(app.query(UserPane)) == 0


async def test_unknown_command_names_the_known_commands_and_starts_no_run() -> None:
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/foo", "enter")
        await settle(pilot, lambda: len(app.query(SystemPane)) == 1, "the feedback line mounts")

        line = app.query_one(SystemPane).render().plain
        assert "/foo" in line
        assert "/model" in line
        assert "/quit" in line
        assert input_queue.empty()
        assert len(app.query(UserPane)) == 0


async def test_model_command_typed_in_full_switches_and_confirms() -> None:
    switch = FakeSwitch()
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue, model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/model gemma3:27b", "enter")
        await settle(pilot, lambda: len(app.query(SystemPane)) == 1, "the confirmation mounts")

        assert switch.switched == ["gemma3:27b"]
        assert app.query_one(SystemPane).render().plain == "model → gemma3:27b"
        assert input_queue.empty()


async def test_model_command_without_an_argument_reports_the_current_model() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/model", "escape", "enter")
        await settle(pilot, lambda: len(app.query(SystemPane)) == 1, "the line mounts")

        assert switch.switched == []
        assert app.query_one(SystemPane).render().plain == "model → qwen3:8b"


async def test_message_with_a_slash_beyond_the_first_character_reaches_the_core() -> None:
    input_queue: queue.Queue[str] = queue.Queue()
    app = PicoApp(Bus(), input_queue)
    async with app.run_test() as pilot:
        await pilot.pause()
        await submit(app, pilot, "read a/b.txt")
        await settle(pilot, lambda: len(app.query(UserPane)) == 1, "the user pane mounts")

        assert input_queue.get_nowait() == "read a/b.txt"
        assert len(app.query(SystemPane)) == 0


def _menu(app: PicoApp) -> CommandMenu:
    return app.query_one(CommandMenu)


def _labels(app: PicoApp) -> list[str]:
    return [row.label for row in _menu(app).rows]


async def test_typing_slash_shows_both_commands_with_descriptions() -> None:
    app = PicoApp(Bus(), queue.Queue(), model_switch=FakeSwitch())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press("/")
        await settle(pilot, lambda: _menu(app).display, "the menu appears")

        assert _labels(app) == ["quit", "model"]
        rendered = _menu(app).render().plain
        assert "switch the model for the next run" in rendered
        assert "exit pico" in rendered
        assert _menu(app).selection == "quit"


async def test_narrowing_to_one_command_preselects_it_and_enter_runs_it() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/q")
        await settle(pilot, lambda: _labels(app) == ["quit"], "the menu narrows to quit")

        assert _menu(app).selection == "quit"
        await pilot.press("enter")
        await settle(pilot, lambda: not app.is_running, "the app exits")


async def test_enter_on_a_stale_menu_accepts_the_typed_command_not_the_displayed_one() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/q")
        await settle(pilot, lambda: _labels(app) == ["quit"], "the menu catches up")

        stale = complete("/", None)
        assert stale is not None
        _menu(app).show(stale)
        _menu(app).move(1)
        assert _menu(app).selection == "model"

        await pilot.press("enter")
        await settle(pilot, lambda: not app.is_running, "quit runs")

    assert switch.switched == []


async def test_accepting_model_enters_the_argument_stage_with_the_live_model_list() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/mo", "enter")
        await settle(pilot, lambda: text_input.text == "/model ", "the argument stage opens")

        await settle(pilot, lambda: _labels(app) == ["qwen3:8b", "gemma3:27b"], "models listed")
        assert switch.switched == []


async def test_argument_stage_narrows_and_marks_the_current_model() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/model ")
        await settle(pilot, lambda: _labels(app) == ["qwen3:8b", "gemma3:27b"], "models listed")

        assert f"qwen3:8b {CURRENT_GLYPH}" in _menu(app).render().plain

        await pilot.press(*"gem")
        await settle(pilot, lambda: _labels(app) == ["gemma3:27b"], "the list narrows")
        assert _menu(app).selection == "gemma3:27b"

        await pilot.press("enter")
        await settle(pilot, lambda: switch.switched == ["gemma3:27b"], "the switch happens")
        assert text_input.text == ""
        assert not _menu(app).display


async def test_up_and_down_move_the_selection_without_moving_the_cursor() -> None:
    app = PicoApp(Bus(), queue.Queue(), model_switch=FakeSwitch())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press("/")
        await settle(pilot, lambda: _menu(app).display, "the menu appears")
        cursor = text_input.cursor_location

        await pilot.press("down")
        await settle(pilot, lambda: _menu(app).selection == "model", "the selection moves down")
        assert text_input.cursor_location == cursor

        await pilot.press("up")
        await settle(pilot, lambda: _menu(app).selection == "quit", "the selection wraps back")
        assert text_input.cursor_location == cursor


async def test_arrow_keys_move_the_cursor_when_the_menu_is_closed() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"ab", "ctrl+j", *"cd")
        await pilot.pause()
        assert text_input.cursor_location == (1, 2)

        await pilot.press("up")
        await pilot.pause()

        assert not _menu(app).display
        assert text_input.cursor_location == (0, 2)


async def test_escape_dismisses_the_menu_and_leaves_the_typed_text() -> None:
    app = PicoApp(Bus(), queue.Queue(), model_switch=FakeSwitch())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/mo")
        await settle(pilot, lambda: _menu(app).display, "the menu appears")

        await pilot.press("escape")
        await settle(pilot, lambda: not _menu(app).display, "the menu dismisses")
        assert text_input.text == "/mo"


async def test_deleting_back_past_the_slash_hides_the_menu() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press("/")
        await settle(pilot, lambda: _menu(app).display, "the menu appears")

        await pilot.press("backspace")
        await settle(pilot, lambda: not _menu(app).display, "the menu hides")


async def test_a_non_slash_first_character_never_shows_the_menu() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"read a/b.txt")
        await pilot.pause()

        assert not _menu(app).display


async def test_model_list_failure_renders_an_error_row_and_the_input_keeps_working() -> None:
    switch = FakeSwitch(names=(), error="connection refused")
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/model ")
        await settle(
            pilot,
            lambda: "connection refused" in _menu(app).render().plain,
            "the error row appears",
        )

        await pilot.press(*"qwen3:8b", "enter")
        await settle(pilot, lambda: switch.switched == ["qwen3:8b"], "the typed name still works")
        assert text_input.text == ""


async def test_opening_the_model_menu_fetches_once_and_reopening_fetches_fresh() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/model ")
        await settle(pilot, lambda: _labels(app) == ["qwen3:8b", "gemma3:27b"], "models listed")

        await pilot.press(*"gem")
        await settle(pilot, lambda: _labels(app) == ["gemma3:27b"], "the cached names narrow")
        assert switch.available_calls == 1

        await pilot.press("escape")
        await settle(pilot, lambda: not _menu(app).display, "the menu hides")

        await pilot.press("backspace")
        await settle(pilot, lambda: switch.available_calls == 2, "reopening fetches fresh")


async def test_keystrokes_during_a_pending_fetch_stay_responsive_until_names_land() -> None:
    switch = GatedSwitch([Options(names=("qwen3:8b", "gemma3:27b"))])
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/model ")
        await settle(
            pilot, lambda: FETCHING_LABEL in _menu(app).render().plain, "the pending row shows"
        )

        await pilot.press(*"gem")
        await pilot.pause()
        assert text_input.text == "/model gem"
        assert FETCHING_LABEL in _menu(app).render().plain
        assert _menu(app).selection is None
        assert switch.calls == 1

        switch.gates[0].set()
        await settle(
            pilot, lambda: _labels(app) == ["gemma3:27b"], "the names land without further input"
        )


async def test_a_superseded_fetch_is_discarded_not_queued() -> None:
    switch = GatedSwitch([Options(names=("stale:1",)), Options(names=("fresh:1",))])
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#user-input", ChatInput).focus()
        await pilot.press(*"/model ")
        await settle(pilot, lambda: switch.calls == 1, "the first fetch starts")

        await pilot.press("escape")
        await settle(pilot, lambda: not _menu(app).display, "the menu hides")

        await pilot.press("f")
        await settle(pilot, lambda: switch.calls == 2, "a second fetch runs despite the first")

        switch.gates[1].set()
        await settle(pilot, lambda: _labels(app) == ["fresh:1"], "the fresh names render")

        switch.gates[0].set()
        await pilot.pause()
        await pilot.pause()
        assert _labels(app) == ["fresh:1"]


async def test_every_registered_command_is_dispatched() -> None:
    switch = FakeSwitch()
    for command in COMMANDS:
        app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.run_command(f"/{command.name}")
            await pilot.pause()

            unknown = [
                pane for pane in app.query(SystemPane) if "unknown command" in pane.render().plain
            ]
            assert unknown == [], f"/{command.name} is registered but not dispatched"


async def test_tab_completes_the_highlighted_command_without_running_it() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/q")
        await settle(pilot, lambda: _labels(app) == ["quit"], "the menu narrows to quit")

        await pilot.press("tab")
        await settle(pilot, lambda: not _menu(app).display, "the completion lands")

        assert text_input.text == "/quit"
        assert app.is_running
        assert app.focused is text_input

        await pilot.press("enter")
        await settle(pilot, lambda: not app.is_running, "enter then runs the completion")


async def test_tab_on_a_command_taking_an_argument_opens_the_argument_stage() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/mo")
        await settle(pilot, lambda: _labels(app) == ["model"], "the menu narrows to model")

        await pilot.press("tab")
        await settle(pilot, lambda: text_input.text == "/model ", "the argument stage opens")

        assert text_input.cursor_location == (0, len("/model "))
        await settle(pilot, lambda: _labels(app) == ["qwen3:8b", "gemma3:27b"], "models listed")
        assert switch.switched == []
        assert app.focused is text_input


async def test_tab_completes_the_selected_model_argument() -> None:
    switch = FakeSwitch()
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/model gem")
        await settle(pilot, lambda: _labels(app) == ["gemma3:27b"], "the list narrows")

        await pilot.press("tab")
        await settle(pilot, lambda: text_input.text == "/model gemma3:27b", "the name completes")

        assert switch.switched == []
        assert app.focused is text_input


async def test_tab_with_the_menu_closed_moves_focus_and_shift_tab_is_untouched() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"hello")
        await pilot.pause()
        assert not _menu(app).display

        await pilot.press("tab")
        await pilot.pause()
        assert text_input.text == "hello"
        assert app.focused is not text_input

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is text_input


async def test_tab_on_an_already_complete_command_changes_nothing() -> None:
    app = PicoApp(Bus(), queue.Queue())
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/quit")
        await settle(pilot, lambda: _labels(app) == ["quit"], "the menu narrows to quit")

        await pilot.press("tab")
        await pilot.pause()
        await pilot.pause()

        assert text_input.text == "/quit"
        assert app.focused is text_input
        assert app.is_running


async def test_tab_on_a_menu_with_no_rows_leaves_the_text_and_focus_alone() -> None:
    switch = FakeSwitch(names=(), error="connection refused")
    app = PicoApp(Bus(), queue.Queue(), model_switch=switch)
    async with app.run_test() as pilot:
        await pilot.pause()
        text_input = app.query_one("#user-input", ChatInput)
        text_input.focus()
        await pilot.press(*"/model ")
        await settle(
            pilot,
            lambda: "connection refused" in _menu(app).render().plain,
            "the error row appears",
        )

        await pilot.press("tab")
        await pilot.pause()
        await pilot.pause()

        assert text_input.text == "/model "
        assert app.focused is text_input
        assert switch.switched == []
