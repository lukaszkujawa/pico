# SQL Surface

The vision names SQL as the deterministic computation surface, but the model's only computation route today is `shell`. Filtering, aggregation, and arithmetic get done by a small model composing awk/sort/paste pipelines — precisely the failure mode Pico exists to remove. The runtime already speaks SQLite; the model should too.

Two tools close the gap. `load_table` ingests a CSV file into a scratch database without the data ever passing through the model's context — handles instead of payloads, applied to tabular data. `sql` runs arbitrary SQL against that scratch database, so filtering, joins, and totals become one declarative statement the runtime executes exactly. Results are recorded like any tool result, so they become citable facts and inherit the handle machinery for free.

## Design decisions

* **Scratch database, not the session log.** Each session gets its own scratch SQLite file next to the session database, named by session id, opened lazily on first use. The events table is never exposed to model-written SQL. Delegates do not get these tools; computation belongs to the main loop.
* **`load_table(path, table)`.** The runtime parses the CSV (`csv` module, first row is the header, column names sanitised to valid identifiers), creates or replaces the table, and returns the table name, column names, and row count — a schema summary, never the rows. A column whose every non-empty value parses as a number is stored numeric; everything else is text.
* **`sql(query)`.** Executes one statement against the scratch database and returns rows rendered as an aligned text table, capped at 50 rows with a `+N more rows` note. SQLite errors return as tool errors with the message verbatim — that is the feedback loop that lets the model fix its query. A progress handler aborts runaway statements after a few seconds.
* **Teach through the tool specs.** The `sql` and `load_table` descriptions say plainly: prefer this over shell pipelines for anything tabular or numeric. No system-prompt growth.

## [ ] T001 load_table

### Description

Implement the scratch database (lazy, per-session, path derived from the session database path) and the `load_table` tool per the design decisions.

### Acceptance criteria

* `tests/core/test_actions.py` covers: loading a CSV creates the table with sanitised column names and correct row count; numeric columns come back as numbers from a query and text columns as text; reloading the same table name replaces it; a missing file and a malformed header return tool errors; the result contains the schema summary and no data rows.
* Fully annotated, passes strict Pyright.

## [ ] T002 sql

### Description

Implement the `sql` tool per the design decisions: aligned rendering, row cap, verbatim SQLite errors, progress-handler timeout.

### Acceptance criteria

* `tests/core/test_actions.py` covers: a SELECT with joins and aggregates over loaded tables returns correct rendered rows; results beyond the cap truncate with the correct `+N more rows` note; an invalid statement returns the SQLite message as a tool error; a statement over the events table of the session log fails (wrong database); CREATE/INSERT statements work so the model can stage intermediate results.
* Fully annotated, passes strict Pyright.

## [ ] T003 Integrate and measure

### Description

Register both tools in `register_actions` (not `register_delegate_actions`). Add one eval task where the answer requires grouped aggregation over a CSV too large to read whole — the shape where shell pipelines fail and one GROUP BY succeeds — with a deterministic check. Run `make check`; if a live model is configured, run `make evals` and record before/after in the completion notes, watching `aggregate_large_file`, `multi_step_chore`, and `dependent_chain`.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.

### Completion

Commit:
