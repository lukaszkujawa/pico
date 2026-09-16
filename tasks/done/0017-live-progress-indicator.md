# Live Progress Indicator

The `WaitingIndicator` spinner (`0006-tui-redesign.md`) tells you the agent is doing *something*, but not how long it's taken or how much work has gone into it. Claude Code's own status line solves this with a live elapsed-time counter and a running token count next to the spinner while it's working. This milestone brings the same feedback to Pico: while a turn is in flight, show elapsed seconds ticking up and a live (approximate, then reconciled) token count, updating in real time — not just a spinner glyph.

Token counts aren't streamed incrementally by the LLM — Ollama reports `prompt_tokens`/`completion_tokens` only once a call finishes, via `GenerationComplete` (`src/pico/llm/types.py`), which `stream_step` (`src/pico/core/loop.py`) currently discards (`case GenerationComplete(): pass`). So the live count during streaming is an estimate — using the same `estimate_tokens` heuristic already in `src/pico/core/context.py` (`len(text) // 4`) against the assistant text/thinking deltas as they arrive — reconciled against the real `completion_tokens` the moment each call in the turn completes, the same way a running counter settles once real data lands.

## [X] T001 Emit real token counts from completed calls

### Description

In `src/pico/core/events.py`, extend `GenerationComplete` handling in `stream_step` (`src/pico/core/loop.py`): instead of discarding `GenerationComplete`, publish a new bus event `GenerationCompleted(prompt_tokens: int | None, completion_tokens: int | None)` (or reuse fields already on `GenerationComplete` if simpler — your call) so the TUI can hear about a real token count the instant a single LLM call in the turn finishes, without waiting for the whole turn to end.

Keep `RunStarted`/`RunFinished` semantics unchanged — this is an additional event published mid-turn (once per LLM call, since a turn can involve several: tool-call round-trips, delegate calls), not a replacement for them.

### Acceptance criteria

* `tests/core/test_loop.py` covers: a scripted multi-call turn (e.g. one tool call then a final answer) publishes one token-count event per LLM call, with the token values taken directly from that call's `GenerationComplete`, not accumulated by loop.py itself (accumulation belongs in the TUI, see T003).
* Fully annotated, passes strict Pyright.

## [X] T002 `ElapsedTimer` widget

### Description

In `src/pico/tui/widgets.py`, add `ElapsedTimer`, a small `Static` following the existing `WaitingIndicator` shape (`start()`/`stop()`, an internal `Timer`, a `reactive` driving `render()`):

* `start()` records a start time (`time.monotonic()`) and begins a `set_interval` tick (once per second is enough — this doesn't need spinner-grade smoothness) that updates a reactive elapsed-seconds value.
* `stop()` stops the interval; the widget keeps showing the final elapsed time rather than resetting, matching how `ToolCallPane`'s spinner freezes on its last frame rather than disappearing.
* `render()` formats elapsed time as `Ns` under a minute, `MmNs` at or above a minute (e.g. `47s`, `1m12s`) — no need to handle hours.

### Acceptance criteria

* `tests/tui/test_widgets.py` covers: `start()` then repeated `pilot.pause()` ticks show increasing elapsed values; `stop()` freezes the displayed value; the minute-boundary formatting switch (`59s` -> `1m00s` or similar) is covered without needing to actually wait 60 real seconds (monkeypatch `time.monotonic` or drive the reactive directly — your call, whichever keeps the test fast and deterministic).
* Fully annotated, passes strict Pyright.

## [X] T003 Live token counter alongside the spinner

### Description

Extend `WaitingIndicator` (or compose it with `ElapsedTimer` and a new token counter into one row — pick whichever keeps `src/pico/tui/app.py`'s mounting code simplest) so that while a turn is running, the user sees all three together: spinner, elapsed time, and a token count, e.g. `⠋ 12s · ~340 tokens`.

The token count:

* Increments live from an estimate (`estimate_tokens` from `src/pico/core/context.py`, applied to the delta text as it streams — the app already receives this in `src/pico/tui/app.py`'s `on_assistant_pane_delta` and `on_thinking_pane_delta` handlers) so it visibly climbs during generation, not just once at the end.
* Snaps to the real number the moment a `GenerationCompleted` event (T001) arrives for that call, replacing the estimate for the tokens generated so far with ground truth, then continues estimating on top of that baseline for any further streaming in the same turn.
* Prefix the estimate with `~` while no real count has landed yet for the current segment; drop the `~` once reconciled.
* Resets to zero at the start of each new turn (`RunStarted`), same as `ElapsedTimer` restarting via `start()`.

### Acceptance criteria

* `tests/tui/test_app.py` covers: publishing `AssistantTextDelta`/`AssistantThinkingDelta` events increases the displayed estimate live (before any `GenerationCompleted`); a subsequent `GenerationCompleted` snaps the count to the real value; a second turn (`RunStarted` again) resets the counter to zero rather than continuing to accumulate across turns.
* Fully annotated, passes strict Pyright.

## [X] T004 Wire elapsed time and token count into the run lifecycle

### Description

In `src/pico/tui/app.py`, start `ElapsedTimer` (T002) and the token counter (T003) exactly when `WaitingIndicator.start()` is called (`on_user_input_submitted`), and stop both exactly when `WaitingIndicator.stop()` is called (every place that already calls it: `on_run_finished_message`, `on_run_cancelled_message`, `on_error_message`, and the first-pane-created paths). Don't duplicate the stop-call sites — factor a single helper if that keeps it from being repeated four times.

### Acceptance criteria

* `tests/tui/test_app.py` covers: submitting input starts all three (spinner, elapsed, tokens) together; a run finishing, being cancelled, or erroring stops all three together, with the final elapsed time and token count still visible (not blanked) until the next turn starts.
* Fully annotated, passes strict Pyright.

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Manually run `uv run python -m pico` (or `make run`) against a real backend, send a message that triggers at least one tool call before the final answer, and confirm: the elapsed timer visibly ticks up in real time, the token count climbs during streaming, and both freeze at sensible final values once the turn completes.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.
* The manual run shows a live-updating elapsed time and token count, not a static readout that only appears once the turn ends.

### Completion

Commit:
