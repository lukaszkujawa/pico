# Stuckness and Streaks

Roadmap milestone 7 of 7 (see `0008-session-store.md` for the full arc).

The circuit-breaker layer, added last and deliberately — it only makes sense once there's a real loop with real actions producing a real event log to analyze (`0008`-`0010` at minimum). Ports the old architecture's two-tier design: soft nudges (streak-triggered text injected into the next prompt, e.g. "you've investigated twice with no new content, act or delegate now") and a hard stop (`assess()`-style stuckness detection: no progress in N steps, repeated identical actions, consecutive tool failures), both computed by pure functions over the session event log (`0008`) rather than loop-local mutable counters — matching the "no hidden state, everything derived fresh from the log" principle throughout the old architecture.

Depends on `0009` (needs stop-condition hooks in the declarative loop config to actually terminate a run on hard stuckness) and benefits from `0010`/`0013` existing so there are enough distinct action/delegate outcomes for the signals (repeated-action, tool-failure-streak, delegate-rejection-streak) to be meaningful rather than trivial on a two-action vocabulary.

Keep the signal set small initially — pick the 2-3 highest-value signals from the old architecture's five (e.g. steps-without-progress and repeated-action-count) rather than porting all five plus context-churn tracking in one pass.

## Design decisions

* **Two signals, both derived fresh from `Session.events()`.** No loop-local mutable counters — a signal is a pure function `Session -> int`, matching `pico.core.ledger.facts`/`goal`'s existing style:
  * `repeated_action_streak(session) -> int` — length of the trailing run of `ToolCallRecorded` events (from the end of the log, skipping over interleaved `AssistantMessageRecorded` events — the loop always records one before each tool call, so treating it as a breaker would make the streak count useless in practice) that all share the same `(name, arguments)` as the very last one. Broken by a `UserMessageRecorded` (a genuine new user turn) or by the log being empty/not ending in a tool call, in which case it is `0`.
  * `tool_failure_streak(session) -> int` — same trailing-walk shape, counting consecutive trailing `ToolCallRecorded` events with `is_error=True`, also skipping interleaved `AssistantMessageRecorded` events and broken by `UserMessageRecorded`. `0` if the last tool call succeeded or there is none yet.
* **One assessment function, two tiers.** `src/pico/core/stuckness.py` exposes `assess(session: Session) -> Stuckness`, where `Stuckness` is a frozen dataclass: `repeated_action_streak: int`, `tool_failure_streak: int`, `nudge: str | None`, `stuck: bool`. `nudge` is set (soft tier) once either streak reaches `NUDGE_THRESHOLD = 2`, with wording naming the specific streak (repeated action vs failures) so the model gets an actionable hint, not a generic warning. `stuck` is `True` (hard tier) once either streak reaches `STUCK_THRESHOLD = 6`. Thresholds are module constants, sized above `MAX_INVALID_ACTION_ATTEMPTS`'s existing 5-attempt budget would already catch a pure failure loop, but a repeated-*identical*-action loop (same call, alternating success/failure) is not caught by that counter today, so this is a genuinely new signal, not a duplicate of `0010`'s cap.
* **Wiring, not a new `StepOutcome`.** A new step handler `stuckness_step(runner: LoopRunner) -> StepOutcome`, inserted first in `DEFAULT_LOOP_CONFIG.steps` (before `stream_step`), computes `assess(runner.session)` each iteration. If `stuck`, returns `"done"` — the existing `LoopConfig` short-circuit-on-non-continue mechanism (`0009`) is exactly the "stop-condition hook" the milestone intro refers to; no new outcome value or runner API needed. Otherwise stores the computed nudge (or `None`) on the runner as transient state (`runner.pending_nudge: str | None`, same pattern as `pending_tool_calls`) and returns `"continue"`.
* **Nudge injection is render-time only, never persisted.** A nudge is a hint derived fresh from the log each step, not a durable fact — it must not be written to the session as a fake `UserMessageRecorded` (that would corrupt the conversation history `0008`/`0012` treat as ground truth). Instead, `stream_step` appends the pending nudge as one extra `Message(role=Role.USER, content=nudge)` after `render_messages(...)`'s output, only for that LLM call, when `runner.pending_nudge` is not `None`. This keeps `Session`'s log exactly what actually happened (user turns, assistant turns, tool calls) while still letting the model see the nudge on its next turn.

## [X] T001 Streak functions over the session log

### Description

In `src/pico/core/stuckness.py` (new module):

* `repeated_action_streak(session: Session) -> int` — walk `session.events()` results from the end backwards, skipping `AssistantMessageRecorded` events, counting consecutive trailing `ToolCallRecorded` events whose `(name, arguments)` all equal the last one's. A `UserMessageRecorded` stops the walk. Returns `0` if the log is empty or ends in something other than a tool call once `AssistantMessageRecorded` events are skipped.
* `tool_failure_streak(session: Session) -> int` — same trailing-walk shape (skip `AssistantMessageRecorded`, stop at `UserMessageRecorded`), counting consecutive trailing `ToolCallRecorded` events with `is_error=True`. Returns `0` if the log is empty or the last tool call succeeded.
* Both are pure functions of `session.events()` — no caching, no mutable state, consistent with `pico.core.ledger.facts`/`goal`.

### Acceptance criteria

* `tests/core/test_stuckness.py` (new) covers: empty session gives `0` for both; a single successful tool call gives `repeated_action_streak == 1`, `tool_failure_streak == 0`; three identical consecutive tool calls (same name+arguments) followed by a different call resets `repeated_action_streak` to `1`; three consecutive failing calls give `tool_failure_streak == 3`; a success breaking a failure run resets `tool_failure_streak` to `0`; an intervening `UserMessageRecorded` between two identical tool calls prevents them from being counted as one streak; an `AssistantMessageRecorded` between two identical tool calls (the loop's normal shape) does *not* break the streak.
* Fully annotated, passes strict Pyright.

## [X] T002 `Stuckness` and `assess()`

### Description

Still in `src/pico/core/stuckness.py`:

* Module constants `NUDGE_THRESHOLD = 2`, `STUCK_THRESHOLD = 6`.
* `Stuckness` frozen dataclass: `repeated_action_streak: int`, `tool_failure_streak: int`, `nudge: str | None`, `stuck: bool`.
* `assess(session: Session) -> Stuckness` — computes both streaks via T001's functions. `stuck` is `True` iff either streak `>= STUCK_THRESHOLD`. `nudge` is `None` if neither streak `>= NUDGE_THRESHOLD`; otherwise a short instruction string naming the specific streak, e.g. for a repeated-action streak: `f"you've repeated the same action {n} times with no new result — try something different or use delegate/answer"`; for a failure streak: `f"the last {n} tool calls failed — reconsider your approach instead of retrying the same way"`. If both streaks independently cross `NUDGE_THRESHOLD`, the repeated-action nudge takes precedence (it's the more specific signal). `nudge` is still computed (non-`None`) even when `stuck` is also `True` — `assess` never has a reason to omit information the caller didn't ask for; whether a nudge is actually used when the run is already stopping is the loop-wiring's call (T003), not `assess`'s.

### Acceptance criteria

* `tests/core/test_stuckness.py` covers: below both thresholds gives `nudge is None`, `stuck is False`; a repeated-action streak crossing `NUDGE_THRESHOLD` but below `STUCK_THRESHOLD` gives a non-`None` nudge naming the repeat count and `stuck is False`; a streak reaching `STUCK_THRESHOLD` gives `stuck is True`; a failure streak crossing `NUDGE_THRESHOLD` gives a nudge naming failures; when both streaks cross `NUDGE_THRESHOLD` simultaneously, the repeated-action wording is chosen.
* Fully annotated, passes strict Pyright.

## [X] T003 Wire stuckness into the loop

### Description

In `src/pico/core/loop.py`:

* Add `runner.pending_nudge: str | None = None` to `LoopRunner.__init__`, alongside the existing transient-state fields (`pending_tool_calls`, etc.).
* Add `stuckness_step(runner: LoopRunner) -> StepOutcome`: computes `assess(runner.session)`. If `result.stuck`, return `"done"` (hard stop — no bus event or session write beyond what already exists; the model simply doesn't get another turn). Otherwise set `runner.pending_nudge = result.nudge` and return `"continue"`.
* Change `stream_step` to build its message list as `render_messages(runner.session, runner.context_size)` plus, when `runner.pending_nudge is not None`, one appended `Message(role=Role.USER, content=runner.pending_nudge)` — this combined list (not the bare `render_messages` output) is what gets passed to `runner.llm.stream(...)`. The nudge is not written to `runner.session` and is not part of `session.messages()`/`session.events()` afterward.
* `DEFAULT_LOOP_CONFIG = LoopConfig(steps=(stuckness_step, stream_step, tool_call_step))` — stuckness check runs first each iteration, before any LLM call is made, so a hard-stuck run doesn't spend a step on a doomed generation.
* The delegate sub-loop (`_run_delegate`'s inner `LoopConfig`) does **not** get `stuckness_step` — it already has its own bound (`MAX_DELEGATE_STEPS`) and the milestone's read-only, single-question sub-agent has too short a horizon for streak-based stuckness to be a meaningful signal; adding it there would be scope creep beyond "keep the signal set small."

### Acceptance criteria

* `tests/core/test_loop.py` covers: a run where the model keeps calling the same tool with identical arguments hits `STUCK_THRESHOLD` and ends via `"done"`/`RunFinished()` (not `max_steps`, not `MAX_INVALID_ACTION_ATTEMPTS`) — construct the scenario so the repeated-action streak is what trips, not the invalid-action counter (e.g. identical *valid* tool calls); a run with a nudge-triggering-but-not-stuck streak asserts the recording LLM client's second call received a message list whose last message is `Role.USER` with the expected nudge content, and that `session.events()`/`session.messages()` afterward contains no trace of that nudge text; a run that never crosses `NUDGE_THRESHOLD` behaves exactly as before (no extra message appended, matching pre-milestone recorded call shapes).
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` to re-export the new public surface (`Stuckness`, `assess`, `repeated_action_streak`, `tool_failure_streak`, `stuckness_step`, `NUDGE_THRESHOLD`, `STUCK_THRESHOLD`).

### Acceptance criteria

* `make check` passes with no errors.
* `grep -rn "stuckness_step" src/pico/core/loop.py` shows it wired into `DEFAULT_LOOP_CONFIG` and absent from the delegate sub-loop's `LoopConfig`.

### Completion

Commit:
