from pico.core.loop.dispatch import tool_call_step
from pico.core.loop.generate import generation_step
from pico.core.loop.policy import policy_step
from pico.core.loop.runner import LoopConfig, LoopRunner, Step
from pico.core.loop.subruns import step_orchestration_step

DEFAULT_LOOP_STEPS: tuple[Step, ...] = (
    policy_step,
    step_orchestration_step,
    generation_step,
    tool_call_step,
)
DEFAULT_LOOP_CONFIG = LoopConfig(steps=DEFAULT_LOOP_STEPS)

__all__ = ["DEFAULT_LOOP_CONFIG", "DEFAULT_LOOP_STEPS", "LoopRunner"]
