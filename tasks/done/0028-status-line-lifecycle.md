# Status Line Lifecycle

Reported as: "the animated icon indicating model work gets messed up on first tool call and gets stuck somewhere above and never spins again."

Two separate defects produce that, both confirmed by driving the real app.

**The spinner stops permanently on the first pane.** `on_assistant_pane_create` and `on_thinking_pane_create` (`src/pico/tui/app.py`) call `self._stop_spinner()`, which calls `WaitingIndicator.stop()` — setting `display = False` and cancelling the timer. Nothing ever calls `start()` again for the rest of the turn. The intent was "stop the spinner once real output arrives", but the model is still working for the whole remainder of the run: during tool execution (a 30s shell command), and during every subsequent generation. After the first thinking delta the status row shows a frozen elapsed time and token count with no activity indicator at all.

**The status line is stranded mid-transcript.** `StatusLine` is composed as a child of `#conversation` (`PicoApp.compose`), so it is an ordinary scrolling sibling of the panes. `on_user_input_submitted` calls `conversation.move_child(status, after=-1)` to push it to the bottom, but *nothing moves it again as panes mount*. Every pane mounted after that point is appended below it. Probe output after one thinking pane and one tool pane:

```
Splash / UserPane / StatusLine / ThinkingPane / ToolCallPane
```

The status line — and its spinner — is left buried above the output, matching "stuck somewhere above". Repeated `move_child` calls on every mount would be the wrong fix: it is O(n) churn per delta and still scrolls away.

## Design decisions

* **The status line docks, it does not scroll.** Move `StatusLine` out of `#conversation` and into the bottom `#footer` (above `InputBar`), where it is always visible and never reordered. This deletes every `move_child(status, after=-1)` call and the `action_new_session` special-casing that skips it while clearing children.
* **The spinner tracks the run, not the first pane.** It starts when a turn starts and stops only on `RunFinished`, `RunCancelled`, or `ErrorOccurred`. `_stop_spinner` and the `stop_spinner()` method are deleted along with their calls in the pane-create handlers.
* **Keep the spinner honest during tool calls.** The `ToolCallPane`'s own inline spinner (`widgets.py` `_advance`) indicates *that call* is running; the status spinner indicates *the turn* is running. Both spinning at once is correct and is not the bug — the bug is the status one being dead.
* **One timer cadence.** `WaitingIndicator` and `ToolCallPane` both `set_interval(0.08, ...)`; leave that as is. Do not add a shared global ticker — it is not needed and adds a concept.

## [X] T001 Dock the status line in the footer

### Description

In `src/pico/tui/app.py`, compose `StatusLine` inside `#footer` above `InputBar` instead of inside `#conversation`. Remove both `conversation.move_child(status, after=-1)` calls (`on_user_input_submitted`, `action_new_session`) and simplify `action_new_session`'s child-clearing loop, which no longer needs to exclude the status line. Add the CSS needed to keep it flush above the input bar.

### Acceptance criteria

* `tests/tui/test_app.py` covers: after mounting several panes, `StatusLine` is not a child of `#conversation` and is a child of `#footer`; the status line remains visible (`display is True`) after panes mount that would previously have been appended below it; the existing status-row reflow test still passes at small and large terminal sizes.
* Fully annotated, passes strict Pyright.

## [X] T002 Spinner runs for the whole turn

### Description

Delete `PicoApp._stop_spinner` and `StatusLine.stop_spinner`, and the `self._stop_spinner()` calls in `on_assistant_pane_create` and `on_thinking_pane_create`. The spinner now stops only where `_stop_status` is already called: `on_run_finished_message`, `on_run_cancelled_message`, `on_error_message`.

### Acceptance criteria

* `tests/tui/test_app.py` covers: the spinner is still running after an assistant pane, a thinking pane, and a tool-call pane have all been created; it is still running while a tool call is open (between `ToolCallStarted` and `ToolCallFinished`); it stops on `RunFinished`, on `RunCancelled`, and on `ErrorOccurred`; elapsed time and token count continue to update across the whole turn.
* The existing tests `test_first_pane_create_stops_waiting_indicator` and `test_first_pane_stops_the_spinner_but_keeps_elapsed_and_tokens_running` assert the old, wrong behaviour and are rewritten to assert the new contract.
* `grep -rn "stop_spinner" src tests` shows no remaining references.
* Fully annotated, passes strict Pyright.

## [X] T003 Spinner restarts on a following turn

### Description

Ensure a second turn in the same session restarts the spinner. `on_run_started_message` currently only resets the token counter; the status row is started from `on_user_input_submitted`, which does not fire for a queued message that starts running later.

### Acceptance criteria

* `tests/tui/test_app.py` covers: submitting a message while a run is in flight, then finishing the first run and receiving a second `RunStarted`, leaves the spinner running and the elapsed timer restarted from zero for the second turn.
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until green.

### Acceptance criteria

* `make check` passes, including the coverage floor.

### Completion

Commit:
