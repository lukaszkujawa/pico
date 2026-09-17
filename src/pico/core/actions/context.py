import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pico.core.actions.shape import ResultShape
from pico.core.bus import Bus
from pico.llm.client import LLMClient
from pico.llm.types import ToolSpec
from pico.session import Session

if TYPE_CHECKING:
    from pico.core.actions.delegate import Delegate


@dataclass(frozen=True)
class AnswerOutcome:
    content: str
    result: str
    is_error: bool
    accepted: bool
    reason: str | None
    verify: str | None


ActionResult = AnswerOutcome | tuple[str, bool] | None

Spawn = Callable[["Delegate"], tuple[str, bool]]


@dataclass
class ActionContext:
    session: Session
    llm: LLMClient
    bus: Bus
    pane_id: str
    cancel: threading.Event
    context_size: int
    chars_per_token: float
    result_shape: ResultShape | None
    spawn: Spawn
    final_answer: str | None = field(default=None, init=False)


@dataclass(frozen=True)
class RunnerAction:
    spec: ToolSpec
    execute: Callable[[ActionContext, Mapping[str, object]], ActionResult]
