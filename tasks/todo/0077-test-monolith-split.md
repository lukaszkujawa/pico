# Test Monolith Split

The test tree follows one-module-one-file everywhere except its two largest files. `tests/tui/test_app.py` (1856 lines) holds at least six unrelated concerns — pane rendering from bus events, scroll pinning, input submission and queueing, cancel and new-session controls, the activity and stats readouts, and the slash-command menu — and `tests/core/test_generate.py` (1114 lines) mixes generation streaming with prompt overhead accounting, plan-message assembly, chars-per-token reconciliation, and budget-overflow reporting, several of which pin functions that live in `loop/prompt.py`, not `loop/generate.py`. Monoliths on one axis cost real money: a failure in either file names a haystack, unrelated changes churn the same file, and the sheer size invites tests that reach into internals because the observable seam is buried. The repo already has the pattern to follow — `tests/llm_fakes.py` and `tests/core/loop_fixtures.py` hold shared fakes, every other test file matches one concern.

## Design decisions

* **Split by concern, matching the granularity elsewhere.** `tests/tui/test_app.py` becomes six files: `test_panes.py` (assistant, thinking, tool-call, and answer panes from bus events), `test_scrolling.py` (pinning, overflow, layout at sizes, splash), `test_input.py` (input bar, submission, queueing, initial prompt), `test_controls.py` (escape-cancel and ctrl+n new-session), `test_activity.py` (spinner, elapsed, token counter, stats strip, request counter, meter), and `test_menu.py` (slash commands, completion menu, dispatch). `tests/core/test_generate.py` becomes three: `test_generate.py` keeps streaming, pane-id, cancellation, and `record` verdict tests; `test_generation_prompting.py` takes what the runner sends — briefing and plan messages, overhead, budget-exceeded, the bounded long run; and the pure `reconcile` ratio tests move into `tests/core/test_prompt.py` beside the module that owns the function.
* **Shared helpers become a fixtures module, not copies.** Helpers used across the new files (`_submit`, the recording handles and fake switches in the TUI file; the recording client and overhead helpers in the generate file) move to `tests/tui/app_fixtures.py` and grow `tests/core/loop_fixtures.py`, following the existing fakes-module precedent. Nothing is duplicated between the new files.
* **This is a move, not a rewrite — with one licensed exception.** Test bodies move verbatim by default. A test may change only when it demonstrably pins internals rather than behaviour — asserting private attributes or widget instance identity where an observable outcome (rendered text, published message, enqueued input) exists — and then only to re-anchor the same scenario on the observable seam. A test may be deleted only when another test in the new layout already covers the identical behaviour; every rewrite and deletion is listed in the completion notes.
* **The suite's strength is the invariant.** Coverage stays at or above the configured `fail_under = 94`, the total behaviour covered does not shrink, and no production code changes. If a moved test fails, the move exposed order-dependence — fix the test's isolation, never the source.
* **Scope guard.** No changes under `src/`, no conftest growth beyond what exists, no reorganising test files outside these two, no new pytest plugins or markers, no renaming of the surviving `test_generate.py` scope beyond the extractions named above.

## [ ] T001 Split the TUI app tests

### Description

Break `tests/tui/test_app.py` into the six concern files, moving shared helpers into `tests/tui/app_fixtures.py`.

### Acceptance criteria

* `tests/tui/test_app.py` no longer exists; the six named files hold every surviving test, each file's tests exercising only its named concern.
* Shared helpers live in `tests/tui/app_fixtures.py` with no helper duplicated across files.
* Any rewritten or deleted test is listed in the completion notes with the internals it was pinned to or the test that already covers it.
* Coverage remains at or above the configured threshold; `make check` passes.

## [ ] T002 Split the generation tests

### Description

Break `tests/core/test_generate.py` into the retained `test_generate.py` and new `test_generation_prompting.py`, moving the `reconcile` tests into `tests/core/test_prompt.py` and shared helpers into `tests/core/loop_fixtures.py`.

### Acceptance criteria

* `test_generate.py` holds only streaming, pane-id, cancellation, and `record` tests; `test_generation_prompting.py` holds the sent-prompt, overhead, plan-message, and budget tests; the `reconcile` tests sit in `test_prompt.py`.
* Shared helpers live in `loop_fixtures.py` with no duplication between the files.
* Any rewritten or deleted test is listed in the completion notes as in T001.
* Coverage remains at or above the configured threshold; `make check` passes.
