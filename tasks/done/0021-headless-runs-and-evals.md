# Headless Runs and Evals

`VISION.md` draws a hard line: "Complexity in the runtime is justified only when it measurably improves the ability of smaller models to solve harder tasks." Nothing in the repository measures anything. The ledger, the context budget, stuckness nudges, delegates — every distinctive mechanism was built on judgment, and there is no way to learn whether any threshold (`NUDGE_THRESHOLD = 2`? `COMPLETION_RESERVE_FRACTION = 0.25`?) helps or hurts a Qwen-class model. Every milestone after this one should be able to answer "did it move the numbers?"

Two things are missing. First, a headless entry point: `run_pico` (`src/pico/app.py`) is welded to the TUI — there is no way to run one task programmatically and get a result back. Second, an eval suite: a fixed set of tasks with deterministic pass/fail checks (checked by code, never by a model — the vision's verification principle applied to Pico itself), run against a live model, producing a metrics record that can be compared across runs and across harness changes.

The suite needs a live LLM, so it stays out of `make check`; the *machinery* (headless runner, task execution, checking, reporting) gets ordinary unit tests against the scripted LLM-client pattern `tests/core/test_loop.py` already uses.

## Design decisions

* **`src/pico/headless.py`.** `run_turn(llm, session, context_size, prompt) -> TurnResult` — builds the registry (`register_actions`), appends the `UserMessageRecorded`, runs a `LoopRunner` with `DEFAULT_LOOP_CONFIG` to completion synchronously (no threads, no queues — the TUI needed those, this doesn't), and returns a frozen `TurnResult`: `answer: str | None`, `iterations: int`, `tool_calls: int`, `prompt_tokens: int`, `completion_tokens: int`, `duration_seconds: float`, `error: str | None`. Counts come from subscribing a collector to the run's `Bus` (tool calls, errors) and from `GenerationComplete` token fields — if `0017`'s per-call token event exists by now, consume it; otherwise wrap the client the way `LoggingLLMClient` (`src/pico/debug/log.py`) does. Missing token counts sum as zero.
* **Tasks are code, not config.** `src/pico/evals/tasks.py` defines a frozen `EvalTask`: `name: str`, `prompt: str`, `setup: Callable[[Path], None]`, `check: Callable[[Path, str], bool]` — `setup` populates a fresh working directory, `check` inspects that directory and the final answer and returns pass/fail deterministically. No YAML, no DSL.
* **One task, one world.** The runner (`src/pico/evals/runner.py`) gives each task a fresh temp working directory (`setup` runs first), a fresh session id in a throwaway database, and changes the process cwd to the task directory for the duration of the turn (the file/shell tools resolve relative paths against process cwd; the headless path is single-threaded so `os.chdir` in a try/finally is sufficient — note it and move on).
* **Report to read and to diff.** `uv run python -m pico.evals` runs the suite against the configured model (`load_config()`), prints a table — task, pass/fail, iterations, tool calls, tokens, seconds — and writes the same data plus config (model, context size) as JSON to `eval_results/<timestamp>.json` (directory gitignored). Exit code 0 regardless of pass rate; evals report, they don't gate. A `make evals` target wraps it.
* **The suite must stress the harness, not just the model.** At least two tasks must produce tool output large enough to force handle truncation at a realistic `LLM_CONTEXT_SIZE`, so `read_fact` and the budget machinery are on the critical path; at least one must require multiple dependent steps (write code, run it, fix it) so stuckness and long-horizon behavior matter. A suite of one-shot trivia would measure the model, and the point is to measure the runtime.

## [X] T001 Headless turn runner

### Description

Implement `src/pico/headless.py` per the design decisions. Reuse existing wiring (`_build_llm_client` stays in `app.py` or moves somewhere shared — your call, no duplication).

### Acceptance criteria

* `tests/test_headless.py` covers, all with scripted LLM clients: a turn ending in `answer` yields that answer with correct iteration/tool-call counts; a turn ending by stuckness or step cap yields `answer is None`; token totals sum multiple `GenerationComplete` events across a multi-call turn; an `LLMError` surfaces in `error`, not as an exception.
* Fully annotated, passes strict Pyright.

## [X] T002 Task model and eval runner

### Description

Implement `EvalTask`, the per-task isolation (temp dir, fresh session, cwd swap), sequential suite execution, the printed table, and the JSON report writer in `src/pico/evals/`. `src/pico/evals/__main__.py` loads config, builds the real client, and runs the registered suite.

### Acceptance criteria

* `tests/evals/test_runner.py` covers, with fake tasks and a scripted client: `setup` runs before the turn in the task's own directory; `check` receives that directory and the final answer; a `check` returning `False` (and a `check` raising) records a fail without stopping the suite; the JSON report contains one entry per task with the `TurnResult` metrics; cwd is restored even when a task blows up.
* Fully annotated, passes strict Pyright.

## [X] T003 Initial suite

### Description

Register the first suite in `src/pico/evals/tasks.py` — roughly eight tasks, graduated, each with a mechanical `check`. Suggested shape (adjust freely, keep the coverage intent):

1. Read one seeded file and answer with a value from it, citation required.
2. Search several seeded files for where a value is defined; answer names the right file.
3. Write a specified file; `check` reads it back.
4. Run a seeded script and report its output.
5. Aggregate across a seeded file too large for the budget (forces handle + `read_fact`); `check` verifies the computed value.
6. Answer a question about a large log file where the evidence sits past the truncation preview (forces recall, not preview-guessing).
7. Write a small program until a seeded test script passes; `check` reruns the script.
8. A multi-file refactor or multi-step chore requiring 5+ dependent tool calls; `check` verifies the end state.

### Acceptance criteria

* Every task's `setup`/`check` pair is exercised by a unit test that fabricates the passing end state directly (no LLM) and asserts `check` accepts it, plus one wrong state it rejects — a broken checker is worse than no checker.
* Tasks 5 and 6 are verified (in those same tests) to seed content whose rendered size exceeds `prompt_budget` at a documented reference context size.
* Fully annotated, passes strict Pyright.

## [X] T004 Make target and docs

### Description

Add `make evals`. Document in `README.md`: what the suite is for, how to run it against a configured model, where results land, and the convention that runtime-behavior milestones should quote before/after suite results in their completion notes when a live model is available.

### Acceptance criteria

* A reader with a working `.env` can run the suite and find their results file from the README alone.

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green (eval machinery is covered by the scripted-client tests; nothing in `make check` may require a live model). If a live Ollama endpoint is configured and reachable, run `make evals` once and commit nothing from `eval_results/` — just confirm the report writes and the table renders; if no endpoint is reachable, skip the live run and say so in the commit message.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.
* `eval_results/` is gitignored.

### Completion

Commit:
