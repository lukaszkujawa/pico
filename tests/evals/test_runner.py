import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pico.config import Config
from pico.evals.runner import EvalOutcome, render_table, run_suite, run_task, write_report
from pico.evals.tasks import EvalTask
from pico.headless import TurnResult
from pico.llm.types import (
    GenerationComplete,
    Message,
    StreamEvent,
    ToolCall,
    ToolCallReady,
    ToolSpec,
)


class AnsweringClient:
    def __init__(self, answer: str = "done") -> None:
        self._answer = answer
        self.cwds: list[Path] = []

    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        self.cwds.append(Path.cwd())
        yield ToolCallReady(
            tool_call=ToolCall(
                id="a", name="answer", arguments={"content": self._answer, "citations": []}
            )
        )
        yield GenerationComplete(finish_reason="tool_calls", prompt_tokens=5, completion_tokens=2)


def _config() -> Config:
    return Config(
        vendor="ollama",
        base_url="http://localhost:11434",
        model="qwen3",
        api_key=None,
        context_size=8192,
        session_path=":memory:",
    )


def _noop_setup(directory: Path) -> None:
    return None


def _always_pass(directory: Path, answer: str) -> bool:
    return True


def _always_fail(directory: Path, answer: str) -> bool:
    return False


def _task(
    name: str,
    setup: Callable[[Path], None] = _noop_setup,
    check: Callable[[Path, str], bool] = _always_pass,
) -> EvalTask:
    return EvalTask(name=name, prompt="do it", setup=setup, check=check)


def test_setup_runs_before_the_turn_in_the_tasks_own_directory() -> None:
    client = AnsweringClient()
    seen: list[Path] = []

    def setup(directory: Path) -> None:
        seen.append(directory)
        (directory / "seed.txt").write_text("value")

    def check(directory: Path, answer: str) -> bool:
        return (directory / "seed.txt").read_text() == "value"

    outcomes = run_suite(client, _config(), [_task("seeded", setup=setup, check=check)])

    assert outcomes[0].passed
    assert seen == client.cwds


def test_check_receives_the_task_directory_and_final_answer() -> None:
    seen: list[tuple[Path, str]] = []

    def setup(directory: Path) -> None:
        (directory / "seed.txt").write_text("value")

    def check(directory: Path, answer: str) -> bool:
        seen.append((directory, answer))
        return (directory / "seed.txt").read_text() == "value"

    outcomes = run_suite(
        AnsweringClient("the answer"), _config(), [_task("checked", setup=setup, check=check)]
    )

    assert outcomes[0].passed
    assert seen[0][1] == "the answer"


def test_failing_check_records_fail_without_stopping_the_suite() -> None:
    outcomes = run_suite(
        AnsweringClient(),
        _config(),
        [
            _task("falsy", check=_always_fail),
            _task("passing"),
        ],
    )

    assert [(outcome.name, outcome.passed) for outcome in outcomes] == [
        ("falsy", False),
        ("passing", True),
    ]


def test_raising_check_propagates_instead_of_recording_a_fail() -> None:
    def raising(directory: Path, answer: str) -> bool:
        raise RuntimeError("checker exploded")

    with pytest.raises(RuntimeError, match="checker exploded"):
        run_task(AnsweringClient(), _config(), _task("raising", check=raising))

    with pytest.raises(RuntimeError, match="checker exploded"):
        run_suite(AnsweringClient(), _config(), [_task("raising", check=raising)])


def test_cwd_is_restored_even_when_a_task_blows_up() -> None:
    def exploding(directory: Path) -> None:
        raise RuntimeError("setup exploded")

    origin = Path.cwd()
    with pytest.raises(RuntimeError):
        run_suite(AnsweringClient(), _config(), [_task("boom", setup=exploding)])

    assert Path.cwd() == origin


class ExplodingClient:
    def stream(self, messages: list[Message], tools: list[ToolSpec]) -> Iterator[StreamEvent]:
        raise RuntimeError("client exploded")
        yield


def test_turn_that_blows_up_is_recorded_as_error_and_cwd_is_restored() -> None:
    origin = Path.cwd()

    outcomes = run_suite(ExplodingClient(), _config(), [_task("boom")])

    assert Path.cwd() == origin
    assert outcomes[0].passed is False
    assert outcomes[0].result.error is not None


def test_report_contains_one_entry_per_task_with_turn_metrics(tmp_path: Path) -> None:
    config = _config()
    outcomes = run_suite(
        AnsweringClient(), config, [_task("first"), _task("second", check=_always_fail)]
    )

    path = write_report(outcomes, config, tmp_path)
    report = json.loads(path.read_text())

    assert report["model"] == "qwen3"
    assert report["context_size"] == 8192
    assert [entry["name"] for entry in report["tasks"]] == ["first", "second"]
    assert [entry["passed"] for entry in report["tasks"]] == [True, False]
    assert report["tasks"][0]["prompt_tokens"] == 5
    assert report["tasks"][0]["completion_tokens"] == 2
    assert report["tasks"][0]["iterations"] == 1
    assert report["tasks"][0]["tool_calls"] == 1
    assert report["tasks"][0]["answer"] == "done"
    assert report["tasks"][0]["error"] is None


def _outcome(name: str, passed: bool) -> EvalOutcome:
    return EvalOutcome(
        name=name,
        passed=passed,
        result=TurnResult(
            answer="a",
            iterations=2,
            tool_calls=3,
            prompt_tokens=100,
            completion_tokens=20,
            duration_seconds=1.25,
            error=None,
        ),
    )


def test_table_lists_every_task_and_a_pass_count() -> None:
    table = render_table([_outcome("alpha", True), _outcome("beta", False)])

    lines = table.splitlines()
    assert lines[0].split() == [
        "task",
        "result",
        "iters",
        "tools",
        "prompt",
        "completion",
        "seconds",
    ]
    assert lines[1].split() == ["alpha", "pass", "2", "3", "100", "20", "1.2"]
    assert lines[2].split() == ["beta", "FAIL", "2", "3", "100", "20", "1.2"]
    assert lines[-1] == "1/2 passed"


def test_main_runs_the_suite_and_writes_a_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import pico.evals.__main__ as main_module

    config = _config()
    monkeypatch.setattr(main_module, "load_config", lambda: config)

    def build(config: Config) -> AnsweringClient:
        return AnsweringClient()

    monkeypatch.setattr(main_module, "build_llm_client", build)
    monkeypatch.setattr(main_module, "SUITE", (_task("solo"),))
    monkeypatch.chdir(tmp_path)

    main_module.main()

    printed = capsys.readouterr().out
    assert "1/1 passed" in printed
    reports = list((tmp_path / "eval_results").iterdir())
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["tasks"][0]["name"] == "solo"
