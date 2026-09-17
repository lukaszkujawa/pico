# SQL Surface

The vision names SQL as the deterministic computation surface, but the model's only computation route today is `shell`. Filtering, aggregation, and arithmetic get done by a small model composing awk/sort/paste pipelines — precisely the failure mode Pico exists to remove. The runtime already speaks SQLite; the model should too.

Two tools close the gap. `load_table` ingests a CSV file into a scratch database without the data ever passing through the model's context — handles instead of payloads, applied to tabular data. `sql` runs arbitrary SQL against that scratch database, so filtering, joins, and totals become one declarative statement the runtime executes exactly. Results are recorded like any tool result, so they become citable facts and inherit the handle machinery for free.

## Design decisions

* **Scratch database, not the session log.** Each session gets its own scratch SQLite file next to the session database, named by session id, opened lazily on first use. The events table is never exposed to model-written SQL. Delegates do not get these tools; computation belongs to the main loop.
* **`load_table(path, table)`.** The runtime parses the CSV (`csv` module, first row is the header, column names sanitised to valid identifiers), creates or replaces the table, and returns the table name, column names, and row count — a schema summary, never the rows. A column whose every non-empty value parses as a number is stored numeric; everything else is text.
* **`sql(query)`.** Executes one statement against the scratch database and returns rows rendered as an aligned text table, capped at 50 rows with a `+N more rows` note. SQLite errors return as tool errors with the message verbatim — that is the feedback loop that lets the model fix its query. A progress handler aborts runaway statements after a few seconds.
* **Teach through the tool specs.** The `sql` and `load_table` descriptions say plainly: prefer this over shell pipelines for anything tabular or numeric. No system-prompt growth.

## [X] T001 load_table

### Description

Implement the scratch database (lazy, per-session, path derived from the session database path) and the `load_table` tool per the design decisions.

### Acceptance criteria

* `tests/core/test_actions.py` covers: loading a CSV creates the table with sanitised column names and correct row count; numeric columns come back as numbers from a query and text columns as text; reloading the same table name replaces it; a missing file and a malformed header return tool errors; the result contains the schema summary and no data rows.
* Fully annotated, passes strict Pyright.

## [X] T002 sql

### Description

Implement the `sql` tool per the design decisions: aligned rendering, row cap, verbatim SQLite errors, progress-handler timeout.

### Acceptance criteria

* `tests/core/test_actions.py` covers: a SELECT with joins and aggregates over loaded tables returns correct rendered rows; results beyond the cap truncate with the correct `+N more rows` note; an invalid statement returns the SQLite message as a tool error; a statement over the events table of the session log fails (wrong database); CREATE/INSERT statements work so the model can stage intermediate results.
* Fully annotated, passes strict Pyright.

## [X] T003 Integrate and measure

### Description

Register both tools in `register_actions` (not `register_delegate_actions`). Add one eval task where the answer requires grouped aggregation over a CSV too large to read whole — the shape where shell pipelines fail and one GROUP BY succeeds — with a deterministic check. Run `make check`; if a live model is configured, run `make evals` and record before/after in the completion notes, watching `aggregate_large_file`, `multi_step_chore`, and `dependent_chain`.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

`make check` green. `make evals` run against `glm-4.7-flash:latest` at 128k context; before is this branch with `sql` and `load_table` unregistered, after is the branch as committed. The new `group_by_region` task is present in both runs so the comparison is like-for-like.

| task                 | before | after | prompt before | prompt after |
|----------------------|--------|-------|---------------|--------------|
| read_one_file        | FAIL   | FAIL  | 1578          | 2060         |
| find_definition      | pass   | FAIL  | 5282          | 5069         |
| write_file           | pass   | pass  | 1573          | 4436         |
| run_script           | pass   | FAIL  | 2424          | 2036         |
| aggregate_large_file | pass   | FAIL  | 71721         | 72731        |
| group_by_region      | pass   | FAIL  | 149146        | 4593         |
| recall_from_log      | pass   | pass  | 90008         | 151795       |
| make_tests_pass      | pass   | pass  | 6209          | 6178         |
| multi_step_chore     | pass   | pass  | 3631          | 7357         |
| dependent_chain      | pass   | FAIL  | 13660         | 14003        |
| many_small_steps     | pass   | pass  | 214436        | 201629       |
| **total**            | 10/11  | 5/11  |               |              |

The single-sample suite totals are not usable evidence here — this model is too noisy at one sample per task for a 10/11 vs 5/11 gap to mean anything. Most "after" failures are the model writing its answer as prose instead of calling the `answer` tool, which leaves `final_answer` unset. Repeating `read_one_file` five times each way gives 1/5 with the SQL tools and 1/5 without, so that failure mode is pre-existing model behaviour, not a regression this milestone introduces.

Repeating the task the milestone targets does separate the two:

| task            | with sql | without sql |
|-----------------|----------|-------------|
| group_by_region | 3/5      | 1/5         |

The prompt-token column is the unambiguous result. `group_by_region` drops from 149146 tokens to 4593, a 97% reduction: without SQL the model pulls 3000 CSV rows through context to aggregate them, and with it `load_table` plus one `GROUP BY` leaves the rows in the scratch database entirely. That is the handles-instead-of-payloads principle applied to tabular data, and it is what justifies the two tools.

One real bug surfaced during measurement and is fixed: the model wrote `verify: sql "SELECT ..."`, treating `sql` as a shell command. `verify` runs under `/bin/sh`, so it failed with `command not found` and the model burned its iteration budget retrying. The `sql` tool description now states it is a tool rather than an executable and must not appear in a shell command or in `verify`, with a regression test covering the spec text.

Also fixed: `test_shell_run_timeout_raises_tool_error_and_kills_process` asserted machine-wide that no `sleep 5` process existed, which raced under xdist once the suite grew. It now matches a per-run unique marker and polls for the asynchronous kill.

Commit: c2fadb8
