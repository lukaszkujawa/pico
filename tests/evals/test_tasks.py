from pathlib import Path

from pico.core.context import (
    estimate_tokens,
    message_text,
    prompt_budget,
    render_messages,
)
from pico.evals.tasks import (
    EXPECTED_INVENTORY_VALUE,
    EXPECTED_LARGEST_REGION,
    EXPECTED_SHIFT_TOTAL,
    EXPECTED_TALLY,
    LOG_MARKER,
    MEASUREMENT_TOTAL,
    REFERENCE_CONTEXT_SIZE,
    SUITE,
    TALLY_FILES,
    EvalTask,
)
from pico.session import Session, ToolCallRecorded, UserMessageRecorded, connect


def _task(name: str) -> EvalTask:
    return next(task for task in SUITE if task.name == name)


def _seeded(name: str, directory: Path) -> EvalTask:
    task = _task(name)
    task.setup(directory)
    return task


def test_suite_names_are_unique_and_every_task_has_a_prompt() -> None:
    assert len({task.name for task in SUITE}) == len(SUITE)
    assert all(task.prompt for task in SUITE)


def test_read_one_file_accepts_the_seeded_port_and_rejects_another(tmp_path: Path) -> None:
    task = _seeded("read_one_file", tmp_path)

    assert "8421" in (tmp_path / "config.ini").read_text()
    assert task.check(tmp_path, "The server listens on port 8421.")
    assert not task.check(tmp_path, "The server listens on port 80.")


def test_find_definition_accepts_the_defining_file_and_rejects_the_importer(
    tmp_path: Path,
) -> None:
    task = _seeded("find_definition", tmp_path)

    assert "LIMIT = 512" in (tmp_path / "beta.py").read_text()
    assert task.check(tmp_path, "LIMIT is defined in beta.py")
    assert not task.check(tmp_path, "LIMIT is defined in gamma.py")
    assert not task.check(tmp_path, "It is defined in beta.py, imported by gamma.py")


def test_write_file_accepts_the_written_file_and_rejects_wrong_content(tmp_path: Path) -> None:
    task = _seeded("write_file", tmp_path)

    assert not task.check(tmp_path, "done")

    (tmp_path / "greeting.txt").write_text("hello from pico\n")
    assert task.check(tmp_path, "done")

    (tmp_path / "greeting.txt").write_text("hello world")
    assert not task.check(tmp_path, "done")


def test_run_script_accepts_the_scripts_checksum_and_rejects_another(tmp_path: Path) -> None:
    task = _seeded("run_script", tmp_path)

    assert (tmp_path / "report.sh").is_file()
    assert task.check(tmp_path, "checksum=93117")
    assert not task.check(tmp_path, "checksum=11111")


def test_aggregate_large_file_accepts_the_true_sum_and_rejects_another(tmp_path: Path) -> None:
    task = _seeded("aggregate_large_file", tmp_path)
    content = (tmp_path / "measurements.csv").read_text()

    computed = sum(int(line.split(",")[2]) for line in content.splitlines())
    assert computed == MEASUREMENT_TOTAL
    assert task.check(tmp_path, f"The readings sum to {MEASUREMENT_TOTAL}.")
    assert not task.check(tmp_path, "The readings sum to 42.")


def test_aggregate_large_file_seeds_more_than_the_reference_prompt_budget(tmp_path: Path) -> None:
    _seeded("aggregate_large_file", tmp_path)

    content = (tmp_path / "measurements.csv").read_text()
    assert estimate_tokens(content) > prompt_budget(REFERENCE_CONTEXT_SIZE)


def test_recall_from_log_accepts_the_fatal_unit_and_rejects_another(tmp_path: Path) -> None:
    task = _seeded("recall_from_log", tmp_path)
    lines = (tmp_path / "service.log").read_text().splitlines()

    fatal = [line for line in lines if "FATAL" in line]
    assert len(fatal) == 1
    assert LOG_MARKER in fatal[0]
    assert task.check(tmp_path, "The retired unit is hd-4471.")
    assert not task.check(tmp_path, "The retired unit is hd-0001.")


def test_recall_from_log_hides_the_evidence_past_budget_and_preview(tmp_path: Path) -> None:
    _seeded("recall_from_log", tmp_path)
    content = (tmp_path / "service.log").read_text()

    assert estimate_tokens(content) > prompt_budget(REFERENCE_CONTEXT_SIZE)
    assert LOG_MARKER not in content[:200]


def test_make_tests_pass_prompt_names_a_command_usable_as_verify() -> None:
    task = next(task for task in SUITE if task.name == "make_tests_pass")

    assert "python3 check.py" in task.prompt
    assert "exits 0" in task.prompt


def test_make_tests_pass_accepts_a_working_solution_and_rejects_a_broken_one(
    tmp_path: Path,
) -> None:
    task = _seeded("make_tests_pass", tmp_path)

    assert not task.check(tmp_path, "done")

    (tmp_path / "solution.py").write_text(
        "def normalise(text: str) -> str:\n    return text.strip().lower()\n"
    )
    assert task.check(tmp_path, "done")

    (tmp_path / "solution.py").write_text("def normalise(text: str) -> str:\n    return text\n")
    assert not task.check(tmp_path, "done")


def test_multi_step_chore_accepts_the_computed_total_and_rejects_wrong_states(
    tmp_path: Path,
) -> None:
    task = _seeded("multi_step_chore", tmp_path)

    counts = dict(
        line.split(",") for line in (tmp_path / "inventory.csv").read_text().splitlines()[1:]
    )
    prices = dict(
        line.split(",") for line in (tmp_path / "prices.csv").read_text().splitlines()[1:]
    )
    assert sum(int(counts[item]) * int(prices[item]) for item in counts) == EXPECTED_INVENTORY_VALUE

    assert not task.check(tmp_path, f"the total is {EXPECTED_INVENTORY_VALUE}")

    (tmp_path / "value.txt").write_text(f"{EXPECTED_INVENTORY_VALUE}\n")
    assert task.check(tmp_path, f"the total is {EXPECTED_INVENTORY_VALUE}")
    assert not task.check(tmp_path, "the total is unclear")

    (tmp_path / "value.txt").write_text("999")
    assert not task.check(tmp_path, f"the total is {EXPECTED_INVENTORY_VALUE}")


def _write_correct_chain_outputs(directory: Path) -> None:
    (directory / "totals.csv").write_text("north,100\nsouth,90\neast,120\nwest,90\n")
    (directory / "summary.txt").write_text(
        f"total {EXPECTED_SHIFT_TOTAL + EXPECTED_SHIFT_TOTAL // 10}, "
        f"biggest region {EXPECTED_LARGEST_REGION}\n"
    )


def test_dependent_chain_accepts_the_full_end_state_and_rejects_partial_work(
    tmp_path: Path,
) -> None:
    task = _seeded("dependent_chain", tmp_path)
    taxed = EXPECTED_SHIFT_TOTAL + EXPECTED_SHIFT_TOTAL // 10

    assert not task.check(tmp_path, f"the total is {taxed}")

    (tmp_path / "totals.csv").write_text("north,100\nsouth,90\neast,120\nwest,90\n")
    assert not task.check(tmp_path, f"the total is {taxed}")

    _write_correct_chain_outputs(tmp_path)
    assert task.check(tmp_path, f"the total is {taxed}")
    assert not task.check(tmp_path, "the total is unclear")


def test_dependent_chain_rejects_wrong_revenues_and_missing_region(tmp_path: Path) -> None:
    task = _seeded("dependent_chain", tmp_path)
    taxed = EXPECTED_SHIFT_TOTAL + EXPECTED_SHIFT_TOTAL // 10
    _write_correct_chain_outputs(tmp_path)

    (tmp_path / "totals.csv").write_text("north,100\nsouth,90\neast,999\nwest,90\n")
    assert not task.check(tmp_path, f"the total is {taxed}")

    (tmp_path / "totals.csv").write_text("north,100\nsouth,90\neast,120\nwest,90\n")
    (tmp_path / "summary.txt").write_text(f"total {taxed}\n")
    assert not task.check(tmp_path, f"the total is {taxed}")


def test_many_small_steps_accepts_the_written_tally_and_rejects_wrong_states(
    tmp_path: Path,
) -> None:
    task = _seeded("many_small_steps", tmp_path)

    values = [
        int((tmp_path / f"part-{index:02d}.txt").read_text().split("value = ")[1])
        for index in range(TALLY_FILES)
    ]
    assert sum(values) == EXPECTED_TALLY

    assert not task.check(tmp_path, f"the tally is {EXPECTED_TALLY}")

    (tmp_path / "tally.txt").write_text(f"{EXPECTED_TALLY}\n")
    assert task.check(tmp_path, f"the tally is {EXPECTED_TALLY}")
    assert not task.check(tmp_path, "the tally is unclear")

    (tmp_path / "tally.txt").write_text("999")
    assert not task.check(tmp_path, f"the tally is {EXPECTED_TALLY}")


def test_many_small_steps_conversation_outgrows_the_budget_without_any_huge_result(
    tmp_path: Path,
) -> None:
    _seeded("many_small_steps", tmp_path)
    parts = [(tmp_path / f"part-{index:02d}.txt").read_text() for index in range(TALLY_FILES)]
    budget = prompt_budget(REFERENCE_CONTEXT_SIZE)

    assert sum(estimate_tokens(part) for part in parts) > budget
    assert all(estimate_tokens(part) < budget // 2 for part in parts)


def test_many_small_steps_renders_within_budget_after_compaction(tmp_path: Path) -> None:
    _seeded("many_small_steps", tmp_path)
    session = Session(connect(":memory:"), "evals")
    session.append(UserMessageRecorded(content="tally the parts"))
    for index in range(TALLY_FILES):
        session.append(
            ToolCallRecorded(
                name="read_file",
                arguments={"path": f"part-{index:02d}.txt"},
                result=(tmp_path / f"part-{index:02d}.txt").read_text(),
                is_error=False,
            )
        )

    rendered = render_messages(session, REFERENCE_CONTEXT_SIZE)
    total = sum(estimate_tokens(message_text(message)) for message in rendered)

    assert total <= prompt_budget(REFERENCE_CONTEXT_SIZE)
