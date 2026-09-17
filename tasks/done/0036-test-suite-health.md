# Test Suite Health

The suite is flaky, and its cost is concentrated where the flakiness is. Three consecutive full runs produced two different single-test failures — `test_run_cancelled_stops_waiting_indicator`, then `test_submitting_input_starts_spinner_elapsed_and_tokens_together` — both in `tests/tui/test_app.py`, both passing when rerun alone: timing flakes under `-n auto` load. The cause is visible in the file: 75 fixed `pilot.pause(0.2)` sleeps that gamble the app has settled, and assertions about spinner/elapsed state that depend on wall-clock timers firing on schedule. The same file owns all 25 slowest tests (0.7–2s each, dominated by those pauses). `tests/test_app.py` has the same disease in thread form: blind `time.sleep(0.1/0.2)` calls, five copy-pasted deadline-polling loops, and an `elapsed < 2` wall-clock assertion. `tests/test_hypothesis_example.py` tests that Python can reverse a string; it is the only use of the `hypothesis` dependency. And every run emits 160–180 warnings — a count that itself varies run to run — so real deprecations are invisible.

A flaky suite trains everyone, humans and unattended agents alike, to rerun until green, which destroys the vision's whole verification contract: `make check` green must mean the change is good, every time.

## Design decisions

* **Wait for conditions, never durations.** One shared helper (suggested: `wait_until(predicate, timeout)` in a `tests` conftest or util module) polls a predicate with a generous deadline and fails with a descriptive message on expiry. It replaces every fixed `pilot.pause(0.2)` (a bare `pilot.pause()` remains fine for flushing already-posted messages) and every blind `time.sleep` in `tests/test_app.py`, including the five hand-rolled polling loops. A generous timeout on a condition is cheap — it only runs out when the test genuinely fails — while a tuned sleep is a flake on a loaded machine and wasted time on an idle one.
* **No wall-clock assertions.** Tests assert state transitions (spinner running/stopped, counter reset, pane closed), never elapsed durations. The `elapsed < 2` shutdown assertion becomes a bounded join whose success is the assertion. Where a TUI test currently waits real time for a timer tick, either drive the timer's callback directly or wait on the state it produces.
* **Delete the useless.** `tests/test_hypothesis_example.py` is removed and `hypothesis` dropped via `uv remove --dev hypothesis`. While touching `tests/tui/test_app.py`, fold tests that differ only in which fixed pause they take into their behavioural neighbours rather than porting them one-to-one — the coverage floor in `pyproject.toml` is the guard that nothing real is lost.
* **Warnings are errors.** `filterwarnings = ["error"]` in the pytest config, with narrow, commented-by-category ignores only for warnings raised inside third-party code that the project cannot fix. New deprecations then fail loudly instead of scrolling past.
* **Proof is repetition.** A de-flaked suite is demonstrated, not asserted: five consecutive full runs must pass. This is the milestone's exit test.

## [X] T001 Delete dead weight, promote warnings

### Description

Remove `tests/test_hypothesis_example.py` and the `hypothesis` dependency. Turn warnings into errors per the design decisions and fix every warning the suite itself causes, adding narrow ignores only for third-party internals.

### Acceptance criteria

* `hypothesis` appears nowhere in `pyproject.toml`, `uv.lock`, or `tests/`.
* The suite passes with `filterwarnings = ["error"]`; any ignore entry names a specific warning category and module, not a blanket suppression.
* `make check` passes with no errors.

## [X] T002 De-flake the TUI tests

### Description

Rework `tests/tui/test_app.py` per the design decisions: condition waits via the shared helper, no fixed-duration pauses, no wall-clock assertions, near-duplicate tests folded together.

### Acceptance criteria

* `grep -rn "pause(0\." tests/` and `grep -rn "time.sleep" tests/tui/` find nothing.
* The two observed flaky tests' behaviours are still covered, asserted on state transitions.
* `uv run pytest tests/tui` total wall time is well under half its current cost, and no single test exceeds one second in the durations report.
* `make check` passes with no errors, including the coverage floor.

## [X] T003 De-flake the threaded app tests

### Description

Rework `tests/test_app.py` per the design decisions: the five polling loops and the blind sleeps replaced by the shared helper, the `elapsed < 2` assertion replaced by a bounded join.

### Acceptance criteria

* `grep -n "time.sleep" tests/test_app.py` finds nothing, and no assertion in the file compares elapsed wall time.
* `make check` passes with no errors, including the coverage floor.

## [X] T004 Prove stability

### Description

Run the full suite five times consecutively; every run must pass. If any run fails, fix the flake and restart the count. Record the five runs' pass lines and total wall time in the completion notes.

### Acceptance criteria

* Five consecutive `uv run pytest` runs pass with zero failures, followed by one green `make check`.

### Completion

Five consecutive `uv run pytest` runs (`-n auto`, the configured default), all green:

```
503 passed in 4.50s
503 passed in 4.41s
503 passed in 4.49s
503 passed in 4.45s
503 passed in 4.48s
```

Because `-n auto` can hide ordering flakes, three serial runs were also taken, all green:
`503 passed in 23.26s`, `503 passed in 23.07s`, `503 passed in 23.36s`. A final `make check`
passed with 97.59% coverage.

Notes:

* `make evals` was not run; it needs a live model and is left to a manual run.
* De-flaking exposed two real production bugs that the old fixed-duration pauses had masked:
  * `Shell.run` never closed the subprocess stdout pipe, leaking a file descriptor per shell
    call. It now runs the process under `with process:`. This was the source of the
    `ResourceWarning: unclosed file` entries.
  * `PicoApp._flush_pending_updates` fires on a 0.05s interval and queried `StatusLine.counter`
    unconditionally. During the window where `StatusLine` exists but has not composed its
    children, `query_one(TokenCounter)` raised `NoMatches` and crashed the app. It now guards on
    the counter's presence, matching the existing `Conversation` guard.
* Test-owned sqlite connections were never closed. Rather than rewriting 49 call sites, an
  autouse `close_sqlite_connections` fixture in `tests/conftest.py` tracks connections opened
  during a test and closes them at teardown; this also covers connections opened inside app code.
* `tests/conftest.py` holds the two shared helpers: `wait_until` for threaded tests and `settle`
  for Textual tests (which polls via `await pilot.pause()`).
* Beyond the named files, `tests/tui/test_widgets.py` had five fixed pauses and
  `tests/core/test_actions.py` a sixth hand-rolled polling loop; both were converted so the
  acceptance greps hold repo-wide.
* `ToolCallPane.spinning` was added so "the timer stopped" is assertable as state instead of by
  observing a frame that does not change over wall-clock time.
* Slowest TUI test fell from 2.00s to 0.50s; `tests/tui` serial wall time from 21.8s to 15.1s.

Commit: d34faa6
