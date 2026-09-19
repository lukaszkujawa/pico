import threading

from textual.pilot import Pilot

from pico.tui.app import ChatInput, PicoApp
from pico.tui.commands import Options
from tests.conftest import settle


async def submit(app: PicoApp, pilot: Pilot[None], text: str = "hi") -> None:
    app.query_one("#user-input", ChatInput).focus()
    await pilot.pause()
    await pilot.press(*text)
    await pilot.press("enter")
    await settle(pilot, lambda: app.query_one("#user-input", ChatInput).text == "", "input clears")


class RecordingCancelHandle:
    def __init__(self) -> None:
        self.trigger_count = 0

    def trigger(self) -> None:
        self.trigger_count += 1


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


class FakeSwitch:
    def __init__(
        self, names: tuple[str, ...] = ("qwen3:8b", "gemma3:27b"), error: str | None = None
    ) -> None:
        self._options = Options(names=names, error=error)
        self.current = "qwen3:8b"
        self.switched: list[str] = []
        self.available_calls = 0

    def available(self) -> Options:
        self.available_calls += 1
        return self._options

    def switch_to(self, model: str) -> None:
        self.switched.append(model)
        self.current = model


class GatedSwitch:
    def __init__(self, results: list[Options]) -> None:
        self.current = "qwen3:8b"
        self.gates = [threading.Event() for _ in results]
        self._results = results
        self.calls = 0
        self.switched: list[str] = []

    def available(self) -> Options:
        index = self.calls
        self.calls += 1
        self.gates[index].wait(timeout=10)
        return self._results[index]

    def switch_to(self, model: str) -> None:
        self.switched.append(model)
        self.current = model
