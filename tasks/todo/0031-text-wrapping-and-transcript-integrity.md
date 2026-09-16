# Text Wrapping and Transcript Integrity

The remaining defects found while auditing the TUI for the screenshot. The first is a visible rendering fault; the rest are correctness bugs in the same layer that have no test coverage.

**Long text is clipped to one line.** `AssistantPane`, `ThinkingPane`, `UserPane` and `AnswerPane` all `render()` a bare `rich.text.Text`. With no wrap configured, Textual measures the widget at its natural height, and a long unbroken run of text renders at **height 1**. Measured against the real app: a 439-character thinking pane rendered as `Size(width=78, height=1)`. This is the truncated grey bars in the screenshot, where a thinking line runs off the right edge mid-sentence ("...The more accurate count"). The panes need explicit wrapping and an auto height.

**Scroll anchoring is inverted.** `on_mount` calls `self.query_one("#conversation", VerticalScroll).anchor()`, but the probe shows the conversation sitting at `scroll_offset=Offset(x=0, y=-12)` with `max_scroll_y=0` — a negative offset, i.e. scrolled above the content. Combined with `0028`'s stranded status line this is why the transcript in the screenshot starts mid-air. Anchoring must hold the view at the bottom as content grows, and must not fight a user who scrolls up deliberately.

**A cancelled run leaves panes open forever.** `stream_step` breaks out of its loop on cancel and publishes the `*Finished` events, but `tool_call_step` has no cancel check at all: once a turn's tool calls start executing, pressing escape cannot stop them, and any `ToolCallPane` already mounted for a call that never completes keeps its spinner timer running indefinitely. `PicoApp` clears its pane dictionaries on `action_new_session` but never calls `finish()` on the still-open panes, so their `set_interval` timers leak for the life of the process.

**The bus consumer thread dies silently.** `PicoApp._consume_bus` runs `for event in self._bus.subscribe()` on a daemon thread with no exception handling. Any error in `translate` — or in `post_message` — kills the thread, and the UI then silently stops receiving *all* events with no indication to the user. Given `0027`'s `DuplicateIds` crash, this is exactly how a single bad event turned into a wholly incoherent transcript.

**`RunFinishedMessage.error` is never read.** `translate` faithfully carries `RunFinished.error` into the message, and `on_run_finished_message` ignores it entirely — a run that ends in an error state reports nothing. `ErrorOccurred` happens to be published alongside it in the `LLMError` path, which masks the omission.

**Dead reactives.** `AssistantPane.finished` and `ThinkingPane.finished` are set by `finish()` and never read by any `render()`. Either render a finished state or delete the reactive; `VISION.md` requires deleting dead code immediately.

## Design decisions

* **Wrapping belongs in the widget, not the caller.** Configure the panes to wrap and size to content rather than having each `render()` hand-break lines. Textual's `Static` already supports this; the fix is styling and `Text` configuration, not a new layout concept.
* **Anchor at the bottom, release on manual scroll.** Keep `anchor()` but ensure it holds the bottom edge; a user who scrolls up should not be yanked back down by the next delta.
* **Cancellation is checked between tool calls, not inside them.** Checking `runner.cancel` at the top of each iteration of `tool_call_step`'s loop is the smallest correct fix. Interrupting a shell command mid-execution is a larger change and is explicitly out of scope here — `0029` T005's `Popen` rewrite is where that becomes possible.
* **A dying bus consumer must be loud.** Wrap the consume loop so an exception surfaces as an `ErrorPane` in the UI rather than silent death. This is a safety net, not a licence to let `translate` throw.

## [ ] T001 Panes wrap long text

### Description

Make `AssistantPane`, `ThinkingPane`, `UserPane` and `AnswerPane` wrap their content and size to the wrapped height instead of rendering a single clipped line.

### Acceptance criteria

* `tests/tui/test_widgets.py` covers: a pane whose content is several times the terminal width reports a rendered height greater than 1 and its full text is present in the render; a pane with an explicit newline still breaks there; a short pane is still height 1.
* `tests/tui/test_app.py` covers: a thinking pane fed a long streamed string at an 80-column size renders taller than one line (regression for the screenshot's clipped grey bars).
* Fully annotated, passes strict Pyright.

## [ ] T002 The transcript stays pinned to the bottom

### Description

Fix the `#conversation` anchoring so new panes keep the newest content in view, with no negative scroll offset, and a deliberate scroll up is not overridden by subsequent deltas.

### Acceptance criteria

* `tests/tui/test_app.py` covers: after mounting enough panes to overflow the viewport, `scroll_offset.y` is never negative and the last-mounted pane is within the visible region; after scrolling up manually, a new delta does not force the view back to the bottom.
* Fully annotated, passes strict Pyright.

## [ ] T003 Cancellation closes open panes

### Description

Check `runner.cancel` between calls in `tool_call_step` (`src/pico/core/loop.py`) and return `"cancelled"` rather than executing the remaining calls. In `src/pico/tui/app.py`, close any still-open panes on `RunCancelled` and on `action_new_session` so their timers stop.

### Acceptance criteria

* `tests/core/test_loop.py` covers: a cancel set before a turn's second tool call leaves that call unexecuted and yields `RunCancelled`; the first call's `ToolCallFinished` is still published.
* `tests/tui/test_app.py` covers: a `ToolCallPane` open when `RunCancelled` arrives ends with a stopped timer and a finished (non-spinning) render; `action_new_session` with an open pane leaves no running timers.
* Fully annotated, passes strict Pyright.

## [ ] T004 A failing bus consumer surfaces

### Description

Make `PicoApp._consume_bus` report a failure into the UI instead of dying silently.

### Acceptance criteria

* `tests/tui/test_app.py` covers: an event that causes the consumer to raise results in a visible error in the conversation rather than a silently dead thread and a frozen UI.
* Fully annotated, passes strict Pyright.

## [ ] T005 Report a run that finished in error, and delete dead reactives

### Description

Use `RunFinishedMessage.error` in `on_run_finished_message` to surface a run that ended badly. Delete the unread `finished` reactives from `AssistantPane` and `ThinkingPane` (and their `finish()` methods if nothing else uses them), or render a finished state — whichever leaves less code.

### Acceptance criteria

* `tests/tui/test_app.py` covers: `RunFinished(error="boom")` with no accompanying `ErrorOccurred` shows the error in the UI; `RunFinished()` with no error shows nothing extra.
* `grep -rn "finished" src/pico/tui/widgets.py` shows no unread reactive remaining.
* Fully annotated, passes strict Pyright.

## [ ] T006 Verify and finalize

### Description

Run `make check` and fix everything until green. Then run the app against a real model and confirm against the original screenshot that: the spinner spins for the whole turn, every box streams, the answer renders as prose, and no text is clipped.

### Acceptance criteria

* `make check` passes, including the coverage floor.

### Completion

Commit:
