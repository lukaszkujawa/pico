# Terminal UI

Build the terminal UI in `src/pico/tui/` using Textual. The TUI is a pure consumer of `pico.core.Bus` events (from `0003-application-core.md`) and never talks to the LLM layer or agentic loop directly. It runs on its own thread, independent of application core.

## [ ] T001 Add Textual dependency

### Description

Add the runtime dependency with `uv add textual`.

### Acceptance criteria

* `textual` appears in `pyproject.toml` under `[project] dependencies`, added only via `uv add`.

## [ ] T002 Color schema

### Description

In `src/pico/tui/theme.py`, define Pico's color schema as a single source of truth: a `Theme` dataclass (or a Textual `design`/`ColorSystem`-compatible structure) with named semantic colors — e.g. background, surface, primary, accent, text, muted text, success, warning, error, and distinct colors for the "thinking" box versus the "tool call" box.

Provide one concrete `PICO_THEME` instance. Every widget in this milestone must reference colors through this theme, never hard-coded hex values inline in widget code.

### Acceptance criteria

* `Theme` is a typed, immutable structure with named semantic fields, no bare strings passed around for colors elsewhere in the TUI package.
* `tests/tui/test_theme.py` confirms `PICO_THEME` provides every required semantic color and that values are valid color strings.
* Fully annotated, passes strict Pyright.

## [ ] T003 Bus-to-TUI message contract

### Description

In `src/pico/tui/messages.py`, define the Textual `Message` subclasses the TUI reacts to, one per `pico.core.BusEvent` variant relevant to rendering (thinking box create/delta/close, tool call box create/delta/close, run started/finished, error). Also define `translate(event: BusEvent) -> Message | None` mapping bus events to TUI messages, so the boundary between core events and Textual's own message system is explicit and in one place.

### Acceptance criteria

* Every `BusEvent` variant from `0003-application-core.md` has a corresponding TUI message or an explicit documented no-op in `translate`.
* `tests/tui/test_messages.py` covers `translate` for each `BusEvent` variant.
* Fully annotated, passes strict Pyright.

## [ ] T004 Thinking box and tool call box widgets

### Description

In `src/pico/tui/widgets.py`, implement two Textual widgets:

* `ThinkingBox` — created empty, appends text as deltas arrive, visually finalizes (e.g. style change) when closed.
* `ToolCallBox` — created with a tool name, appends argument/output content as deltas arrive, finalizes with a success/error style depending on the result.

Both widgets use `PICO_THEME` exclusively for styling and expose a small, explicit API (`append_delta`, `finish`) rather than reaching into Textual internals from outside.

### Acceptance criteria

* Both widgets are reactive: appending a delta updates rendered content without a manual full re-render call from the caller.
* `tests/tui/test_widgets.py` uses Textual's test harness (`App.run_test`) to drive each widget through create -> delta -> delta -> finish and asserts the rendered content and finished state.
* Fully annotated, passes strict Pyright.

## [ ] T005 PicoApp shell and threaded bus consumer

### Description

In `src/pico/tui/app.py`, implement `PicoApp`, a Textual `App` that:

* Takes a `pico.core.Bus` at construction.
* Consumes the bus on a background thread (Textual's `run_worker`/thread-safe `post_message` from a worker thread — do not block the Textual event loop on bus reads), translating each `BusEvent` via `translate` into a Textual message posted to itself.
* Handles each TUI message by creating, updating, or finalizing the appropriate `ThinkingBox`/`ToolCallBox` in a scrolling conversation view, keyed so deltas for an in-progress box update that same widget instance.
* Applies `PICO_THEME` as the app's theme/design.

This widget must not import anything from `pico.llm` or `pico.core.agent` — only `pico.core.Bus` and `pico.core.BusEvent`.

### Acceptance criteria

* `tests/tui/test_app.py` uses `App.run_test`, publishes a sequence of `BusEvent`s onto a real `Bus` from the test (simulating a background publisher thread), and asserts the resulting widget tree reflects them (correct number of boxes, correct finalized content).
* The app never blocks its own UI thread waiting on the bus.
* Fully annotated, passes strict Pyright.

## [ ] T006 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Confirm `src/pico/tui/__init__.py` re-exports the public surface (`PicoApp`, `PICO_THEME`).

### Acceptance criteria

* `make check` passes with no errors.
* Nothing outside `src/pico/tui/` needs to import from `pico.tui.app`, `pico.tui.theme`, `pico.tui.messages`, or `pico.tui.widgets` directly.

### Completion

Commit:
