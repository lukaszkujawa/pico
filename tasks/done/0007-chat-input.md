# Chat Input

Today `run_pico` hardcodes a single message (`"Hello, who are you?"`), runs one `Run` to completion, and the TUI's input bar submission handler (`PicoApp.on_user_input_submitted`) is a no-op — nothing the user types goes anywhere. This milestone makes Pico an actually usable, turn-by-turn chatbot: no tool/agentic loop yet (`ToolRegistry` stays empty), just repeated user message in, assistant message out, forever, driven by real input.

It also fixes a real gap found while building this: `pico.llm.ollama.OllamaClient` only reads `message["content"]` from Ollama's `/api/chat` stream and silently drops `message["thinking"]`. Ollama returns `thinking` as a distinct field from `content` for reasoning-capable models (Qwen3 among them) — the two are streamed separately, not something the client has to infer or parse out of tags. Once that field is captured, "thinking box then the actual answer" is a real, honest distinction end to end, not a UI fiction — resolving the naming question left open in `0006-tui-redesign.md` T001 (`AssistantPane` was deliberately not called a thinking pane because no such signal existed yet; now it will).

## [X] T001 Carry `thinking` through the LLM abstraction

### Description

In `src/pico/llm/types.py`, add a `ThinkingDelta` dataclass (`text: str`) and add it to the `StreamEvent` union.

In `src/pico/llm/ollama.py`, in `_parse_lines`, read `message.get("thinking", "")` alongside `content` and yield a `ThinkingDelta` for it before the `TextDelta`/tool-call handling, only when non-empty — same pattern already used for `content`.

### Acceptance criteria

* `tests/llm/test_types.py` covers `ThinkingDelta` construction and its membership in `StreamEvent`.
* `tests/llm/test_ollama.py` covers a streamed chunk containing both `thinking` and `content` yielding both event kinds in order, and a chunk with only one of them yielding only the corresponding event.
* Fully annotated, passes strict Pyright.

## [X] T002 Distinct thinking events on the bus

### Description

In `src/pico/core/events.py`, add `AssistantThinkingStarted(id)`, `AssistantThinkingDelta(id, text)`, `AssistantThinkingFinished(id)`, mirroring the existing `AssistantText*` trio, and add them to `BusEvent`. Export them from `pico.core.__init__`.

In `src/pico/core/agent.py`, `Run._step` currently opens/closes one text stream per step keyed on the fixed id `"0"`. Extend it to track thinking and text as two independent streams within a step (a step may emit thinking, then content, or interleave — do not assume ordering beyond what the LLM actually sends): on `ThinkingDelta`, open/emit `AssistantThinking*`; on `TextDelta`, open/emit `AssistantText*` as today. Each stream opens on its first delta and closes once, at the end of the step, if it was opened.

Every stream — thinking and text — must get a **fresh, unique id** each time it opens, across the whole `Run`, not the fixed `"0"` used today. Since `Run` will now execute repeatedly across a multi-turn conversation (T005), a fixed id would make the TUI (T003, keyed strictly by id) treat every turn's panes as the same widget and silently overwrite prior turns instead of creating new boxes. Use a simple monotonically increasing counter on `Run` (e.g. `self._next_id`), not a random id — keep it deterministic and easy to test.

### Acceptance criteria

* `tests/core/test_events.py` covers the new event trio's construction and `BusEvent` membership.
* `tests/core/test_agent.py` covers a step that streams thinking deltas followed by text deltas, asserting `AssistantThinkingStarted/Delta.../Finished` then `AssistantTextStarted/Delta.../Finished` are published in order with a shared id per stream, and a step with only text (no thinking) publishes no thinking events at all.
* `tests/core/test_agent.py` covers two full turns (two `_step`-producing exchanges) asserting every opened stream across both turns receives a distinct id — no id is reused.
* Fully annotated, passes strict Pyright.

## [X] T003 Thinking pane in the TUI, and correct pane lifecycle across turns

### Description

In `src/pico/tui/messages.py`, add `ThinkingPaneCreate/Delta/Close` messages and extend `translate` to map the new `AssistantThinking*` bus events to them, alongside the existing `AssistantPane*` mapping for `AssistantText*`.

In `src/pico/tui/theme.py`, add a `thinking_bg` field (and `thinking` foreground/text field) to `Theme` and `PICO_THEME`. The thinking pane must be distinguishable at a glance from the answer pane primarily by **background fill**, not text color alone — reasoning content is visually "inside a different surface" than the final answer, the way a blockquote or code fence reads differently from body prose. Pick a background a clear, deliberate step off the app background (e.g. `surface`, or a new tone between `background` and `surface`), not a near-invisible shade.

In `src/pico/tui/widgets.py`, add a `ThinkingPane` widget using `styles.background = theme.thinking_bg` (mirroring how `ToolCallPane` already sets `styles.border`), quieter/secondary text styling than `AssistantPane`, and the same `append_delta`/`finish` API shape as the other panes.

Box creation must be driven strictly by pane id, exactly as `AssistantPane`/`ToolCallPane` already work — a `*Create` message mounts a new widget keyed by its id into the per-id dict; a `*Delta` looks up that same id and appends; a `*Close` looks up that id and finalizes. This already gives correct behavior across multiple turns since `Run._step` (T002) opens a fresh `AssistantThinkingStarted`/`AssistantTextStarted` with a new id each time a stream opens — confirm this holds by testing a second full turn (thinking -> text -> thinking -> text) and asserting four distinct panes end up in `#conversation`, not two panes being reused or overwritten. Do not add caching or reuse of pane widgets across ids or across turns.

The user's own submitted message currently has no visual representation in the transcript at all — add a minimal `UserPane` (or equivalent), mounted synchronously in `on_user_input_submitted` when the text is enqueued (not via the bus — the user already knows what they typed the instant they hit Enter; no need to round-trip through core for this), so the conversation reads as an actual back-and-forth rather than orphaned assistant panes.

### Acceptance criteria

* `tests/tui/test_theme.py` updated for the new fields.
* `tests/tui/test_messages.py` covers `translate` for the three new event variants.
* `tests/tui/test_widgets.py` covers `ThinkingPane` create -> delta -> delta -> finish, and asserts its background style differs from `AssistantPane`'s.
* `tests/tui/test_app.py` covers: a bus sequence with thinking events followed by text events producing one finished `ThinkingPane` and one finished `AssistantPane`; a second such sequence with new ids producing two additional, separate panes (four total), not reuse of the first turn's widgets; submitting user input immediately mounts a `UserPane` with that text, before any bus event arrives.
* Fully annotated, passes strict Pyright.

## [X] T004 Turn loop: wire submitted input into core

### Description

Replace the single hardcoded `Run` in `run_pico` (`src/pico/app.py`) with a turn loop that responds to real user input:

* Add a simple thread-safe input channel between the TUI and core — a `queue.Queue[str]`, constructed in `run_pico` and passed to `PicoApp`. Do not route user input through `Bus`; `Bus` is a one-way fan-out of `BusEvent`s from core to the TUI, and a request channel is a different concern — keep them separate rather than overloading `Bus`.
* `PicoApp.on_user_input_submitted` puts the submitted text onto that queue (skip empty/whitespace-only submissions) instead of doing nothing.
* In `run_pico`, run a loop on the background thread: block on the queue for the next user message, append it to the running `messages` history, run `Run(llm, tools, bus, messages).execute()`, then wait for the next input. The loop exits when the TUI signals shutdown (see below).
* Drop the hardcoded `"Hello, who are you?"` seed message — the conversation starts empty and waits for the user's first input.
* On TUI exit (`PicoApp.run()` returning), the core thread must not be left blocked forever on an empty queue — signal it to stop (e.g. a sentinel value or `threading.Event` checked between turns) and join it, matching the shutdown contract already established in `0005-integration.md` T002.

### Acceptance criteria

* `tests/test_app.py` covers: submitting one message through the queue results in exactly one `Run` executed with a message history containing it; submitting a second message after the first completes runs a second turn with both messages in history; stopping the TUI does not leave the core thread running (using a fake `LLMClient`, no real Ollama server).
* `tests/tui/test_app.py` covers `on_user_input_submitted` behavior against the queue (a fake/real `queue.Queue` passed to `PicoApp`), including that blank input is not enqueued.
* Fully annotated, passes strict Pyright.

## [X] T005 Animated waiting indicator

### Description

Between the user hitting Enter and the first token of any kind arriving (thinking or text), there is a real gap — the request is in flight and nothing on screen currently shows that. Add an animated indicator that fills exactly that gap.

In `src/pico/tui/widgets.py`, add a `WaitingIndicator` widget: a small `Static` that cycles through a short sequence of frames on a timer (e.g. a braille spinner `⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏` or similar single-glyph animation — pick one, do not build a frame-authoring system for it) using Textual's `set_interval`, styled with `theme.idle` or a new `Theme.waiting` field. Expose `start()`/`stop()` rather than having it self-manage from mount, so `PicoApp` controls its lifecycle explicitly.

In `src/pico/tui/app.py`: mount one `WaitingIndicator` (created once, reused, not recreated per turn) directly below the transcript's last message. Start it in `on_user_input_submitted`, right after mounting the `UserPane` and enqueueing onto the queue from T004. Stop and hide it the moment the first `ThinkingPaneCreate` or `AssistantPaneCreate` arrives for the new turn — whichever comes first — so it never runs concurrently with a streaming pane. If a turn produces neither (e.g. `RunFinished` with no panes ever opened, or an immediate `ErrorMessage`), stop it there too so it never spins forever.

### Acceptance criteria

* `tests/tui/test_widgets.py` covers `WaitingIndicator.start()`/`stop()` changing its running/visible state and that its rendered frame changes over successive timer ticks (advance time via Textual's test clock/`pilot.pause`, not real `sleep`).
* `tests/tui/test_app.py` covers: submitting input starts the indicator; the arrival of the first pane-create event (thinking or text) stops it; an error arriving before any pane is created also stops it. Assert on the indicator's running state, not on animation timing.
* The indicator never overlaps in time with a visible streaming pane for the same turn.
* Fully annotated, passes strict Pyright.

## [X] T006 Cancel an in-flight turn with Escape

### Description

Pressing Escape while a turn is in flight — waiting for the first token, or mid-stream — must stop that turn immediately: no more panes update, the underlying HTTP request to Ollama is actually torn down (not just ignored client-side), and the app returns to idle, ready for new input. Escape while idle (no turn in flight) does nothing.

In `src/pico/core/events.py`, add `RunCancelled()` (no fields) to `BusEvent`. This is not an error — do not route it through `ErrorOccurred`.

In `src/pico/core/agent.py`, give `Run.__init__` a `cancel: threading.Event` parameter. In `_step`'s loop over `self._llm.stream(...)`, check `self._cancel.is_set()` once per received `StreamEvent` and, if set, stop iterating (`break` out of the `for`, which closes the underlying generator/HTTP response via `OllamaClient.stream`'s `with client.stream(...)` context manager exit — do not add separate cancellation plumbing to `OllamaClient`, breaking iteration is sufficient). After breaking, close any panes that were already opened for this step (same `AssistantThinkingFinished`/`AssistantTextFinished` closing already done for normal completion, so the TUI doesn't show a pane stuck mid-stream), publish `RunCancelled()` instead of continuing to the next step or publishing `RunFinished()`, and return from `execute()`.

In `src/pico/app.py`, `run_pico`'s turn loop creates one fresh `threading.Event` per turn (not shared/reused across turns — a stale set event must not cancel the next turn before it starts) and passes it into `Run`. Expose a way for `PicoApp` to reach the current turn's cancel event — the simplest option consistent with the existing `queue.Queue[str]` input channel (T004) is a small shared holder the turn loop updates at the start of each turn and clears at the end (e.g. a single-slot `threading.Event | None` behind the same lock-free pattern already used for the queue, or a tiny `CancelHandle` with `.trigger()`); do not route cancellation through the `Bus`, which remains one-way core-to-TUI.

In `src/pico/tui/app.py`, bind `Escape` (`BINDINGS` or `on_key`) to trigger the current turn's cancel handle, a no-op if no turn is in flight. On `RunCancelled` (via `translate`/a new `RunCancelledMessage`), stop the `WaitingIndicator` (T005) if running and set `StatusHeader` back to idle.

### Acceptance criteria

* `tests/core/test_agent.py` covers: setting the cancel event before/during streaming causes `_step`/`execute` to stop consuming further `StreamEvent`s, publishes `RunCancelled()` (not `RunFinished()`), and closes any panes opened so far for that step.
* `tests/core/test_events.py` covers `RunCancelled` construction and `BusEvent` membership.
* `tests/test_app.py` covers: triggering cancellation mid-turn stops that `Run` (using a fake `LLMClient` that blocks/yields slowly so the test can cancel mid-stream) and the turn loop proceeds to accept the next queued message afterward rather than getting stuck.
* `tests/tui/test_app.py` covers: pressing Escape during a turn triggers the cancel handle exactly once; pressing Escape while idle does nothing (no handle call); receiving `RunCancelled` stops the waiting indicator and resets status to idle.
* Fully annotated, passes strict Pyright.

## [X] T007 Manual smoke test

### Description

With the `.env`-configured Ollama server reachable, run `make run` and confirm interactively: typing a message and pressing Enter sends it, the waiting indicator animates immediately, a thinking pane appears and streams (if the model emits reasoning) followed by the answer pane streaming and finishing, the waiting indicator is gone by the time either pane appears, the input bar is usable again immediately, and a second message continues the same conversation with prior turns as context. Also confirm: pressing Escape while waiting cancels before any pane appears and the app returns to idle; pressing Escape mid-stream stops the pane from receiving further deltas and the app returns to idle; after either cancellation, typing a new message works normally.

### Acceptance criteria

* Smoke test performed and confirmed working; note the model used in the commit message.
* If the configured Ollama server is not reachable from this environment, this task cannot be completed — stop and leave it unchecked rather than marking it done without verification.

## [X] T008 Verify and finalize

### Description

Run `make check` and fix everything until it is green.

### Acceptance criteria

* `make check` passes with no errors.
* Nothing outside `src/pico/tui/` imports from `pico.tui.app`, `pico.tui.theme`, `pico.tui.messages`, or `pico.tui.widgets` directly; nothing outside `src/pico/app.py` imports from all three of `pico.llm`, `pico.core`, and `pico.tui` together.

### Completion

Commit:
