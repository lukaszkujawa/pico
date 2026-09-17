import re
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REFERENCE_CONTEXT_SIZE = 8192


@dataclass(frozen=True)
class EvalTask:
    name: str
    prompt: str
    setup: Callable[[Path], None]
    check: Callable[[Path, str], bool]


def _mentions(answer: str, *needles: str) -> bool:
    lowered = re.sub(r"(?<=\d),(?=\d)", "", answer.lower())
    return all(
        re.search(rf"\b{re.escape(needle.lower())}\b", lowered) is not None for needle in needles
    )


def _setup_read_one_file(directory: Path) -> None:
    (directory / "config.ini").write_text("[server]\nhost = pico.local\nport = 8421\n")


def _check_read_one_file(directory: Path, answer: str) -> bool:
    return _mentions(answer, "8421")


def _setup_find_definition(directory: Path) -> None:
    (directory / "alpha.py").write_text(
        "import beta\n\n\ndef start() -> int:\n    return beta.LIMIT\n"
    )
    (directory / "beta.py").write_text("LIMIT = 512\n")
    (directory / "gamma.py").write_text("from beta import LIMIT\n\nprint(LIMIT)\n")


def _check_find_definition(directory: Path, answer: str) -> bool:
    return _mentions(answer, "beta.py") and not _mentions(answer, "gamma.py")


def _setup_write_file(directory: Path) -> None:
    return None


def _check_write_file(directory: Path, answer: str) -> bool:
    path = directory / "greeting.txt"
    return path.is_file() and path.read_text().strip() == "hello from pico"


def _setup_run_script(directory: Path) -> None:
    (directory / "report.sh").write_text("#!/bin/sh\necho checksum=93117\n")
    (directory / "report.sh").chmod(0o755)


def _check_run_script(directory: Path, answer: str) -> bool:
    return _mentions(answer, "93117")


MEASUREMENT_ROWS = 2400
MEASUREMENT_TOTAL = sum(index % 7 for index in range(MEASUREMENT_ROWS))


def _measurements() -> str:
    lines = [f"sensor-{index:04d},reading,{index % 7}" for index in range(MEASUREMENT_ROWS)]
    return "\n".join(lines) + "\n"


def _setup_aggregate_large_file(directory: Path) -> None:
    (directory / "measurements.csv").write_text(_measurements())


def _check_aggregate_large_file(directory: Path, answer: str) -> bool:
    return _mentions(answer, str(MEASUREMENT_TOTAL))


LOG_LINES = 1800
LOG_MARKER = "FATAL disk controller retired: unit hd-4471"


def _log() -> str:
    lines = [
        f"2026-01-01T00:{index // 60:02d}:{index % 60:02d} INFO heartbeat ok"
        for index in range(LOG_LINES)
    ]
    lines[LOG_LINES // 2] = f"2026-01-01T00:07:30 {LOG_MARKER}"
    return "\n".join(lines) + "\n"


def _setup_recall_from_log(directory: Path) -> None:
    (directory / "service.log").write_text(_log())


def _check_recall_from_log(directory: Path, answer: str) -> bool:
    return _mentions(answer, "hd-4471")


def _setup_make_tests_pass(directory: Path) -> None:
    (directory / "check.py").write_text(
        "from solution import normalise\n\n"
        "assert normalise('  Hello World  ') == 'hello world'\n"
        "assert normalise('PICO') == 'pico'\n"
        "print('ok')\n"
    )


def _check_make_tests_pass(directory: Path, answer: str) -> bool:
    if not (directory / "solution.py").is_file():
        return False
    result = subprocess.run(
        ["python3", "check.py"], cwd=directory, capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0


def _setup_multi_step_chore(directory: Path) -> None:
    (directory / "inventory.csv").write_text("item,count\nbolt,12\nnut,30\nwasher,7\n")
    (directory / "prices.csv").write_text("item,price\nbolt,2\nnut,1\nwasher,3\n")


EXPECTED_INVENTORY_VALUE = 12 * 2 + 30 * 1 + 7 * 3


def _check_multi_step_chore(directory: Path, answer: str) -> bool:
    path = directory / "value.txt"
    if not path.is_file():
        return False
    numbers = re.findall(r"\d+", path.read_text())
    return numbers == [str(EXPECTED_INVENTORY_VALUE)] and _mentions(
        answer, str(EXPECTED_INVENTORY_VALUE)
    )


SHIFT_ORDERS = (
    ("north", 4, 25),
    ("south", 9, 10),
    ("east", 3, 40),
    ("west", 6, 15),
)
EXPECTED_SHIFT_TOTAL = sum(quantity * price for _, quantity, price in SHIFT_ORDERS)
EXPECTED_LARGEST_REGION = max(SHIFT_ORDERS, key=lambda order: order[1] * order[2])[0]


def _setup_dependent_chain(directory: Path) -> None:
    (directory / "orders.csv").write_text(
        "region,quantity,unit_price\n"
        + "".join(f"{region},{quantity},{price}\n" for region, quantity, price in SHIFT_ORDERS)
    )
    (directory / "rate.txt").write_text("tax_percent = 10\n")


def _check_dependent_chain(directory: Path, answer: str) -> bool:
    totals = directory / "totals.csv"
    summary = directory / "summary.txt"
    if not totals.is_file() or not summary.is_file():
        return False
    rows = [line.split(",") for line in totals.read_text().strip().splitlines()]
    expected_rows = [[region, str(quantity * price)] for region, quantity, price in SHIFT_ORDERS]
    if [[cell.strip() for cell in row] for row in rows] != expected_rows:
        return False
    with_tax = EXPECTED_SHIFT_TOTAL + EXPECTED_SHIFT_TOTAL // 10
    return _mentions(summary.read_text(), str(with_tax), EXPECTED_LARGEST_REGION) and _mentions(
        answer, str(with_tax)
    )


TALLY_FILES = 10
TALLY_VALUES = tuple(3 * index + 5 for index in range(TALLY_FILES))
EXPECTED_TALLY = sum(TALLY_VALUES)
TALLY_NOTE_LINES = 60


def _part(index: int, value: int) -> str:
    notes = "\n".join(
        f"# note {line:02d} for part {index:02d}: nothing to total on this line"
        for line in range(TALLY_NOTE_LINES)
    )
    return f"{notes}\nvalue = {value}\n"


def _setup_many_small_steps(directory: Path) -> None:
    for index, value in enumerate(TALLY_VALUES):
        (directory / f"part-{index:02d}.txt").write_text(_part(index, value))


def _check_many_small_steps(directory: Path, answer: str) -> bool:
    path = directory / "tally.txt"
    if not path.is_file():
        return False
    return re.findall(r"\d+", path.read_text()) == [str(EXPECTED_TALLY)] and _mentions(
        answer, str(EXPECTED_TALLY)
    )


SALES_REGIONS = ("north", "south", "east", "west")
SALES_ROWS = 3000


def _sales_rows() -> list[tuple[str, int]]:
    return [
        (SALES_REGIONS[index % len(SALES_REGIONS)], (index % len(SALES_REGIONS)) * 5 + 1)
        for index in range(SALES_ROWS)
    ]


def _sales_totals() -> dict[str, int]:
    totals: Counter[str] = Counter()
    for region, amount in _sales_rows():
        totals[region] += amount
    return dict(totals)


EXPECTED_SALES_TOTALS = _sales_totals()
EXPECTED_TOP_REGION = max(EXPECTED_SALES_TOTALS, key=lambda region: EXPECTED_SALES_TOTALS[region])
EXPECTED_TOP_SALES = EXPECTED_SALES_TOTALS[EXPECTED_TOP_REGION]


def _setup_group_by_region(directory: Path) -> None:
    (directory / "sales.csv").write_text(
        "order_id,region,amount\n"
        + "".join(
            f"{index:05d},{region},{amount}\n"
            for index, (region, amount) in enumerate(_sales_rows())
        )
    )


def _check_group_by_region(directory: Path, answer: str) -> bool:
    return _mentions(answer, EXPECTED_TOP_REGION, str(EXPECTED_TOP_SALES))


SERVICE_TEAMS = (
    ("billing", "invoices", "atlas"),
    ("billing", "refunds", "atlas"),
    ("ingest", "parser", "beacon"),
    ("ingest", "loader", "beacon"),
    ("search", "indexer", "cinder"),
    ("search", "ranker", "cinder"),
    ("search", "cache", "dynamo"),
    ("reporting", "exporter", "ember"),
    ("reporting", "scheduler", "ember"),
    ("gateway", "router", "falcon"),
)
BROKEN_MODULE = "ranker"
EXPECTED_BROKEN_TEAM = next(team for _, module, team in SERVICE_TEAMS if module == BROKEN_MODULE)


def _setup_locate_owner(directory: Path) -> None:
    for service, module, team in SERVICE_TEAMS:
        package = directory / "services" / service / module
        package.mkdir(parents=True)
        (package / "OWNERS").write_text(f"team: {team}\n")
        (package / "module.py").write_text(f"NAME = {module!r}\n")
    incidents = directory / "incidents"
    incidents.mkdir()
    for index, (_, module, _) in enumerate(SERVICE_TEAMS):
        status = "FAILING" if module == BROKEN_MODULE else "ok"
        (incidents / f"run-{index:02d}.log").write_text(
            f"checked module={module}\nstatus={status}\n"
        )


def _check_locate_owner(directory: Path, answer: str) -> bool:
    other_teams = {team for _, _, team in SERVICE_TEAMS} - {EXPECTED_BROKEN_TEAM}
    return _mentions(answer, EXPECTED_BROKEN_TEAM) and not any(
        _mentions(answer, team) for team in other_teams
    )


SUITE: tuple[EvalTask, ...] = (
    EvalTask(
        name="read_one_file",
        prompt="Read config.ini and tell me which port the server listens on.",
        setup=_setup_read_one_file,
        check=_check_read_one_file,
    ),
    EvalTask(
        name="find_definition",
        prompt="Which file in this directory defines LIMIT? Answer with the file name.",
        setup=_setup_find_definition,
        check=_check_find_definition,
    ),
    EvalTask(
        name="write_file",
        prompt="Create a file called greeting.txt containing exactly: hello from pico",
        setup=_setup_write_file,
        check=_check_write_file,
    ),
    EvalTask(
        name="run_script",
        prompt="Run report.sh and tell me the checksum it prints.",
        setup=_setup_run_script,
        check=_check_run_script,
    ),
    EvalTask(
        name="aggregate_large_file",
        prompt=(
            "measurements.csv has one reading per line in its third column. "
            "What is the sum of every reading?"
        ),
        setup=_setup_aggregate_large_file,
        check=_check_aggregate_large_file,
    ),
    EvalTask(
        name="group_by_region",
        prompt=(
            "sales.csv has columns order_id, region and amount. "
            "Which region has the highest total amount, and what is that total? "
            "Answer with the region name and the number."
        ),
        setup=_setup_group_by_region,
        check=_check_group_by_region,
    ),
    EvalTask(
        name="recall_from_log",
        prompt=(
            "service.log contains exactly one FATAL line. "
            "Which hardware unit does it name? Answer with the unit id."
        ),
        setup=_setup_recall_from_log,
        check=_check_recall_from_log,
    ),
    EvalTask(
        name="make_tests_pass",
        prompt=(
            "Write solution.py so that running check.py succeeds. "
            "Running `python3 check.py` exits 0 exactly when the work is correct. "
            "Keep fixing it until check.py prints ok."
        ),
        setup=_setup_make_tests_pass,
        check=_check_make_tests_pass,
    ),
    EvalTask(
        name="multi_step_chore",
        prompt=(
            "inventory.csv lists item counts and prices.csv lists unit prices. "
            "Compute the total value of the inventory, write just that number to value.txt, "
            "and report it."
        ),
        setup=_setup_multi_step_chore,
        check=_check_multi_step_chore,
    ),
    EvalTask(
        name="dependent_chain",
        prompt=(
            "Work through orders.csv step by step. "
            "First write totals.csv with one line per region as region,revenue "
            "where revenue is quantity times unit_price, keeping the original row order "
            "and no header. "
            "Then add the tax percentage in rate.txt to the sum of those revenues. "
            "Then write summary.txt containing that taxed total and the name of the region "
            "with the highest revenue. "
            "Finally report the taxed total."
        ),
        setup=_setup_dependent_chain,
        check=_check_dependent_chain,
    ),
    EvalTask(
        name="locate_owner",
        prompt=(
            "Somewhere under this directory there are incident logs, exactly one of which "
            "records a failing module, and a tree of modules each with an OWNERS file. "
            "Find the failing module and tell me which team owns it. "
            "Answer with the team name only."
        ),
        setup=_setup_locate_owner,
        check=_check_locate_owner,
    ),
    EvalTask(
        name="many_small_steps",
        prompt=(
            f"There are {TALLY_FILES} files named part-00.txt through "
            f"part-{TALLY_FILES - 1:02d}.txt, each holding one value. "
            "Read them one at a time, one file per step, without combining the reads. "
            "Then write the sum of every value to tally.txt and report it."
        ),
        setup=_setup_many_small_steps,
        check=_check_many_small_steps,
    ),
)
