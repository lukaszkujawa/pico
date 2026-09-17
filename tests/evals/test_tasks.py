from pathlib import Path

from pico.core.context import (
    compile_context,
    estimate_tokens,
    message_text,
    prompt_budget,
)
from pico.evals.tasks import (
    AUTHORITATIVE_MARK,
    BROKEN_MODULE,
    DEPOT_NAMES,
    EXPECTED_BROKEN_TEAM,
    EXPECTED_DEPOT_GRAND_TOTAL,
    EXPECTED_DEPOT_TOTALS,
    EXPECTED_INVENTORY_VALUE,
    EXPECTED_LARGEST_REGION,
    EXPECTED_SALES_TOTALS,
    EXPECTED_SHIFT_TOTAL,
    EXPECTED_TALLY,
    EXPECTED_TOP_REGION,
    EXPECTED_TOP_SALES,
    LOG_MARKER,
    MANIFEST_DEPOT,
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


def test_many_small_steps_compiles_within_budget(tmp_path: Path) -> None:
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

    compiled = compile_context(session, REFERENCE_CONTEXT_SIZE)
    total = sum(estimate_tokens(message_text(message)) for message in compiled)

    assert total <= prompt_budget(REFERENCE_CONTEXT_SIZE)


def test_group_by_region_accepts_the_top_group_and_rejects_the_grand_total(tmp_path: Path) -> None:
    task = _seeded("group_by_region", tmp_path)
    rows = [line.split(",") for line in (tmp_path / "sales.csv").read_text().splitlines()[1:]]

    totals: dict[str, int] = {}
    for _, region, amount in rows:
        totals[region] = totals.get(region, 0) + int(amount)
    assert totals == EXPECTED_SALES_TOTALS
    assert max(totals, key=lambda region: totals[region]) == EXPECTED_TOP_REGION

    assert task.check(tmp_path, f"{EXPECTED_TOP_REGION} leads with {EXPECTED_TOP_SALES}.")
    assert not task.check(tmp_path, f"west leads with {sum(EXPECTED_SALES_TOTALS.values())}.")
    assert not task.check(tmp_path, f"north leads with {EXPECTED_TOP_SALES}.")


def test_group_by_region_seeds_more_than_the_reference_prompt_budget(tmp_path: Path) -> None:
    _seeded("group_by_region", tmp_path)

    content = (tmp_path / "sales.csv").read_text()
    assert estimate_tokens(content) > prompt_budget(REFERENCE_CONTEXT_SIZE)


def test_locate_owner_hides_the_answer_behind_exploration(tmp_path: Path) -> None:
    task = _seeded("locate_owner", tmp_path)

    assert BROKEN_MODULE not in task.prompt
    assert EXPECTED_BROKEN_TEAM not in task.prompt
    failing = [path for path in (tmp_path / "incidents").iterdir() if "FAILING" in path.read_text()]
    assert len(failing) == 1
    assert f"module={BROKEN_MODULE}" in failing[0].read_text()
    owners = tmp_path / "services" / "search" / BROKEN_MODULE / "OWNERS"
    assert owners.read_text().strip() == f"team: {EXPECTED_BROKEN_TEAM}"


def test_locate_owner_accepts_the_owning_team_and_rejects_others(tmp_path: Path) -> None:
    task = _seeded("locate_owner", tmp_path)

    assert task.check(tmp_path, EXPECTED_BROKEN_TEAM)
    assert not task.check(tmp_path, "atlas")
    assert not task.check(tmp_path, f"{EXPECTED_BROKEN_TEAM} or maybe dynamo")


def test_locate_owner_accepts_prose_containing_another_team_as_a_substring(
    tmp_path: Path,
) -> None:
    task = _seeded("locate_owner", tmp_path)

    assert task.check(tmp_path, f"remember: the {EXPECTED_BROKEN_TEAM} team owns it")


def test_group_by_region_accepts_thousand_separators_in_the_total(tmp_path: Path) -> None:
    task = _seeded("group_by_region", tmp_path)
    grouped = f"{EXPECTED_TOP_SALES:,}"

    assert task.check(tmp_path, f"{EXPECTED_TOP_REGION} with a total of {grouped}")
    assert not task.check(tmp_path, f"{EXPECTED_TOP_REGION} with a total of {EXPECTED_TOP_SALES}0")


def _totals_report(totals: dict[str, int]) -> str:
    lines = [f"{name},{total}" for name, total in totals.items()]
    return "\n".join([*lines, f"grand_total,{sum(totals.values())}"]) + "\n"


def test_state_carrying_decomposition_ledgers_dwarf_the_reference_window(
    tmp_path: Path,
) -> None:
    _seeded("state_carrying_decomposition", tmp_path)
    ledgers = [(tmp_path / "depots" / name / "ledger.txt").read_text() for name in DEPOT_NAMES]
    budget = prompt_budget(REFERENCE_CONTEXT_SIZE)

    assert sum(estimate_tokens(ledger) for ledger in ledgers) > 4 * budget
    assert all(estimate_tokens(ledger) > budget for ledger in ledgers)


def test_state_carrying_decomposition_hides_the_rule_in_one_depot(tmp_path: Path) -> None:
    _seeded("state_carrying_decomposition", tmp_path)

    manifests = list(tmp_path.rglob("MANIFEST.txt"))
    assert [path.parent.name for path in manifests] == [MANIFEST_DEPOT]
    assert AUTHORITATIVE_MARK in manifests[0].read_text()


def test_state_carrying_decomposition_accepts_the_audited_totals(tmp_path: Path) -> None:
    task = _seeded("state_carrying_decomposition", tmp_path)
    (tmp_path / "audited_totals.txt").write_text(_totals_report(EXPECTED_DEPOT_TOTALS))

    assert task.check(tmp_path, f"the audited grand total is {EXPECTED_DEPOT_GRAND_TOTAL}")
    assert not task.check(tmp_path, "the audited grand total is 12")


def test_state_carrying_decomposition_rejects_totals_that_ignore_the_manifest(
    tmp_path: Path,
) -> None:
    task = _seeded("state_carrying_decomposition", tmp_path)
    unfiltered = {
        name: sum(
            int(line.split("amount=")[1].split()[0])
            for line in (tmp_path / "depots" / name / "ledger.txt").read_text().splitlines()
        )
        for name in DEPOT_NAMES
    }
    (tmp_path / "audited_totals.txt").write_text(_totals_report(unfiltered))

    assert unfiltered != EXPECTED_DEPOT_TOTALS
    assert not task.check(tmp_path, f"the grand total is {sum(unfiltered.values())}")


def test_state_carrying_decomposition_rejects_a_missing_depot(tmp_path: Path) -> None:
    task = _seeded("state_carrying_decomposition", tmp_path)
    partial = dict(list(EXPECTED_DEPOT_TOTALS.items())[:-1])
    (tmp_path / "audited_totals.txt").write_text(
        _totals_report(partial).replace(
            f"grand_total,{sum(partial.values())}", f"grand_total,{EXPECTED_DEPOT_GRAND_TOTAL}"
        )
    )

    assert not task.check(tmp_path, f"the grand total is {EXPECTED_DEPOT_GRAND_TOTAL}")


def test_state_carrying_decomposition_requires_the_report_file(tmp_path: Path) -> None:
    task = _seeded("state_carrying_decomposition", tmp_path)

    assert not task.check(tmp_path, f"the grand total is {EXPECTED_DEPOT_GRAND_TOTAL}")
