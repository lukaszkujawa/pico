import itertools
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from pico.core.actions import ResultShape
from pico.core.bus import Bus
from pico.core.events import ErrorOccurred, RunCancelled, RunFinished, RunStarted
from pico.core.loop.signals import Restrict, Signal
from pico.core.loop.state import DecisionState, DispatchState, GenerationState, StepState
from pico.core.tools import ToolRegistry
from pico.llm.client import LLMClient
from pico.llm.types import ToolCall
from pico.session import Session

StepOutcome = Literal["continue", "done", "cancelled"]

Step = Callable[["LoopRunner"], StepOutcome]

MAX_RUN_STEPS = 250


@dataclass(frozen=True)
class LoopConfig:
    steps: tuple[Step, ...]
    max_steps: int | None = None


class LoopRunner:
    def __init__(
        self,
        llm: LLMClient,
        tools: ToolRegistry,
        bus: Bus,
        session: Session,
        context_size: int,
        config: LoopConfig,
        cancel: threading.Event | None = None,
        id_source: Iterator[int] | None = None,
        result_shape: ResultShape | None = None,
        depth: int = 0,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.bus = bus
        self.session = session
        self.context_size = context_size
        self.config = config
        self.cancel = cancel if cancel is not None else threading.Event()
        self.result_shape = result_shape
        self.depth = depth
        self.error: str | None = None
        self.pending_signal: Signal | None = None
        self.pending_tool_calls: list[ToolCall] = []
        self.tool_call_pane_ids: dict[str, str] = {}
        self.final_answer: str | None = None
        self.dying_of: str | None = None
        self.last_words: bool = False
        self.active_restriction: Restrict | None = None
        self.decision = DecisionState()
        self.generation = GenerationState()
        self.dispatch = DispatchState()
        self.steps = StepState()
        self.iterations = 0
        self._id_source = id_source if id_source is not None else itertools.count()

    @property
    def chars_per_token(self) -> float:
        return self.generation.chars_per_token

    def emit(self, signal: Signal) -> None:
        self.pending_signal = signal

    def take_signal(self) -> Signal | None:
        signal = self.pending_signal
        self.pending_signal = None
        return signal

    def new_id(self) -> str:
        return str(next(self._id_source))

    def fail(self, message: str) -> None:
        self.error = message
        self.bus.publish(ErrorOccurred(message=message))

    def execute(self) -> None:
        self.bus.publish(RunStarted())
        try:
            max_steps = self.config.max_steps
            while max_steps is None or self.iterations <= max_steps:
                if max_steps is not None and self.iterations == max_steps:
                    self.dying_of = f"the generation budget of {max_steps} is spent"
                self.iterations += 1
                outcome = self._run_iteration()
                if outcome == "cancelled":
                    self.bus.publish(RunCancelled())
                    return
                if outcome == "done":
                    break
            if self.last_words and self.final_answer is None:
                self.fail(f"run stopped: {self.dying_of}")
        except Exception as error:
            self.fail(str(error))
        self.bus.publish(RunFinished(error=self.error))

    def _run_iteration(self) -> StepOutcome:
        for step in self.config.steps:
            if self.cancel.is_set():
                return "cancelled"
            outcome = step(self)
            if outcome != "continue":
                return outcome
        return "continue"
