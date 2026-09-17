import itertools

from pico.core.bus import Bus
from pico.core.events import (
    AssistantTextStarted,
    AssistantThinkingStarted,
    ErrorOccurred,
    RunCancelled,
    RunFinished,
    RunStarted,
)
from pico.core.loop import DEFAULT_LOOP_CONFIG
from pico.core.loop.dispatch import tool_call_step
from pico.core.loop.generate import generation_step
from pico.core.loop.policy import (
    budget_step,
    decision_step,
    stuckness_step,
)
from pico.core.loop.runner import MAX_RUN_STEPS, LoopConfig, LoopRunner, StepOutcome
from pico.core.loop.subruns import (
    step_orchestration_step,
)
from pico.llm.errors import LLMError
from pico.llm.types import (
    GenerationComplete,
    TextDelta,
    ThinkingDelta,
)
from pico.session import (
    UserMessageRecorded,
)
from tests.core.loop_fixtures import (
    FailingClient,
    ScriptedClient,
    echo_registry,
    make_session,
)


def test_step_outcome_accepts_each_literal_value() -> None:
    def make(value: StepOutcome) -> StepOutcome:
        return value

    assert make("continue") == "continue"
    assert make("done") == "done"
    assert make("cancelled") == "cancelled"


def test_loop_config_holds_ordered_steps_and_max_steps() -> None:
    def a(runner: LoopRunner) -> StepOutcome:
        return "done"

    def b(runner: LoopRunner) -> StepOutcome:
        return "done"

    config = LoopConfig(steps=(a, b), max_steps=5)

    assert config.steps == (a, b)
    assert config.max_steps == 5


def test_single_always_done_step_publishes_started_and_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def always_done(runner: LoopRunner) -> StepOutcome:
        return "done"

    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        bus,
        make_session(),
        128_000,
        LoopConfig(steps=(always_done,)),
    )
    runner.execute()

    assert next(subscriber) == RunStarted()
    assert next(subscriber) == RunFinished()


def test_continue_n_times_then_done_runs_n_plus_one_iterations() -> None:
    bus = Bus()
    calls: list[int] = []

    def counting(runner: LoopRunner) -> StepOutcome:
        calls.append(1)
        return "continue" if len(calls) <= 2 else "done"

    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        bus,
        make_session(),
        128_000,
        LoopConfig(steps=(counting,)),
    )
    runner.execute()

    assert len(calls) == 3


def test_cancelled_outcome_publishes_cancelled_not_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def always_cancelled(runner: LoopRunner) -> StepOutcome:
        return "cancelled"

    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        bus,
        make_session(),
        128_000,
        LoopConfig(steps=(always_cancelled,)),
    )
    runner.execute()

    assert next(subscriber) == RunStarted()
    assert next(subscriber) == RunCancelled()


def test_step_raising_llm_error_surfaces_as_error_occurred() -> None:
    bus = Bus()
    subscriber = bus.subscribe()

    def raising(runner: LoopRunner) -> StepOutcome:
        raise LLMError("connection lost")

    runner = LoopRunner(
        FailingClient(), echo_registry(), bus, make_session(), 128_000, LoopConfig(steps=(raising,))
    )
    runner.execute()

    assert next(subscriber) == RunStarted()
    assert next(subscriber) == ErrorOccurred(message="connection lost")
    assert next(subscriber) == RunFinished(error="connection lost")


def test_max_steps_reached_without_terminal_outcome_publishes_finished() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    calls: list[int] = []

    def always_continue(runner: LoopRunner) -> StepOutcome:
        calls.append(1)
        return "continue"

    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        bus,
        make_session(),
        128_000,
        LoopConfig(steps=(always_continue,), max_steps=3),
    )
    runner.execute()

    assert len(calls) == 4
    assert next(subscriber) == RunStarted()
    assert next(subscriber) == RunFinished()


def test_steps_after_non_continue_step_are_not_called() -> None:
    bus = Bus()
    calls: list[str] = []

    def first(runner: LoopRunner) -> StepOutcome:
        calls.append("first")
        return "done"

    def second(runner: LoopRunner) -> StepOutcome:
        calls.append("second")
        return "done"

    runner = LoopRunner(
        FailingClient(),
        echo_registry(),
        bus,
        make_session(),
        128_000,
        LoopConfig(steps=(first, second)),
    )
    runner.execute()

    assert calls == ["first"]


def test_shared_id_source_keeps_ids_unique_across_separate_runners() -> None:
    bus = Bus()
    subscriber = bus.subscribe()
    session = make_session()
    session.append(UserMessageRecorded(content="hi"))
    id_source = itertools.count()

    first_client = ScriptedClient(
        [
            [
                ThinkingDelta(text="pondering"),
                TextDelta(text="hello"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )
    first_runner = LoopRunner(
        first_client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, None, id_source
    )
    first_runner.execute()

    second_client = ScriptedClient(
        [
            [
                ThinkingDelta(text="more thoughts"),
                TextDelta(text="world"),
                GenerationComplete(finish_reason="stop"),
            ],
        ]
    )
    second_runner = LoopRunner(
        second_client, echo_registry(), bus, session, 128_000, DEFAULT_LOOP_CONFIG, None, id_source
    )
    second_runner.execute()

    all_events = [next(subscriber) for _ in range(18)]
    started_ids = [
        event.id
        for event in all_events
        if isinstance(event, AssistantThinkingStarted | AssistantTextStarted)
    ]
    assert started_ids == ["0", "1", "2", "3"]
    assert len(set(started_ids)) == len(started_ids)


def test_default_loop_config_is_stuckness_budget_decision_steps_stream_then_tool_call() -> None:
    assert DEFAULT_LOOP_CONFIG.steps == (
        stuckness_step,
        budget_step,
        decision_step,
        step_orchestration_step,
        generation_step,
        tool_call_step,
    )
    assert DEFAULT_LOOP_CONFIG.max_steps == MAX_RUN_STEPS
