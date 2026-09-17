import json
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pico.config import Config
from pico.evals.tasks import EvalTask
from pico.headless import TurnResult, run_turn
from pico.llm.client import LLMClient
from pico.session import Session, connect, new_session_id

RESULTS_DIR = Path("eval_results")


@dataclass(frozen=True)
class EvalOutcome:
    name: str
    passed: bool
    result: TurnResult


def run_task(llm: LLMClient, config: Config, task: EvalTask) -> EvalOutcome:
    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory).resolve()
        task.setup(workdir)
        session = Session(connect(workdir / "session.db"), new_session_id())
        origin = Path.cwd()
        os.chdir(workdir)
        try:
            result = run_turn(llm, session, config.context_size, task.prompt)
        finally:
            os.chdir(origin)
        passed = result.error is None and task.check(workdir, result.answer or "")
    return EvalOutcome(name=task.name, passed=passed, result=result)


def run_suite(llm: LLMClient, config: Config, tasks: Sequence[EvalTask]) -> list[EvalOutcome]:
    return [run_task(llm, config, task) for task in tasks]


_COLUMNS = ("task", "result", "iters", "tools", "prompt", "completion", "seconds")


def render_table(outcomes: Sequence[EvalOutcome]) -> str:
    rows = [
        (
            outcome.name,
            "pass" if outcome.passed else "FAIL",
            str(outcome.result.iterations),
            str(outcome.result.tool_calls),
            str(outcome.result.prompt_tokens),
            str(outcome.result.completion_tokens),
            f"{outcome.result.duration_seconds:.1f}",
        )
        for outcome in outcomes
    ]
    passed = sum(outcome.passed for outcome in outcomes)
    widths = [max(len(cell) for cell in column) for column in zip(_COLUMNS, *rows, strict=True)]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in (_COLUMNS, *rows)
    ]
    return "\n".join([*lines, "", f"{passed}/{len(outcomes)} passed"])


def write_report(
    outcomes: Sequence[EvalOutcome], config: Config, directory: Path = RESULTS_DIR
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.json"
    path.write_text(
        json.dumps(
            {
                "model": config.model,
                "context_size": config.context_size,
                "tasks": [
                    {"name": outcome.name, "passed": outcome.passed, **asdict(outcome.result)}
                    for outcome in outcomes
                ],
            },
            indent=2,
        )
    )
    return path
