# TUI Redesign

`0004-terminal-ui.md` produced a functionally correct but visually plain TUI: a single scrolling column of boxes, one hard-coded dark-blue palette, no chrome, no input. This milestone raises it to the level of a professionally designed terminal application — the visual bar set by tools like `btop` and `k9s` — while keeping the architecture from `0004`: the TUI stays a pure, one-way consumer of `pico.core.Bus` events on its own thread, and never imports `pico.llm` or `pico.core.agent`.

Direction, decided up front:

* A single elegant scrolling conversation transcript, not a multi-pane dashboard. No gauges, sparklines, or resource-monitor widgets — those belong to a different kind of tool.
* The app always fills the entire terminal window and re-renders cleanly on resize.
* Distinct, visually differentiated panes per event kind (assistant text, tool call, error), not one generic box style.
* A fixed input bar pinned to the bottom of the screen at all times: a horizontal rule above it, a horizontal rule below it, and a `>` prompt on the left.
* The color palette is a template: one typed definition swappable for alternate palettes without touching widget code, already true of `Theme` in `0004` — this milestone raises the palette itself to a considered, professional standard and confirms nothing regresses that contract.

## [X] T001 Resolve the assistant-text ambiguity

### Description

`pico.core.events.AssistantTextStarted/Delta/Finished` carries *all* assistant text — there is no separate signal for reasoning/thinking versus a final answer (see `src/pico/core/agent.py`). The `0004` TUI names its widget `ThinkingBox` but it actually renders every assistant message, thinking or not.

Decide and document (in this file, as a short note under this task, and in a docstring-free comment-free form — i.e. plainly in code/tests, not prose comments) how the redesigned TUI represents assistant text without misrepresenting what the core actually distinguishes today:

* Do not invent a fake "thinking vs answering" visual distinction the core cannot back up.
* Rename the widget/pane concept away from "thinking" to something accurate (e.g. an "assistant" pane) unless a corresponding core event distinction is added in this milestone.
* If distinguishing reasoning from final answer is wanted, adding that split is out of scope here — it belongs to `pico.core`, not the TUI. This task's job is to make the TUI honest about the one signal that exists today.

### Acceptance criteria

* No widget or identifier in `pico.tui` claims a "thinking" semantic that the current `BusEvent` set cannot support.
* The naming decision is reflected consistently across `theme.py`, `widgets.py`, `messages.py`, `app.py`.

### Decision

`AssistantText*` carries the model's only text output; there is no core signal distinguishing reasoning from a final answer. Renamed `ThinkingBox` -> `AssistantPane`, `ThinkingBox{Create,Delta,Close}` -> `AssistantPane{Create,Delta,Close}`, and `Theme.thinking` -> `Theme.assistant` throughout `theme.py`, `widgets.py`, `messages.py`, `app.py`, and their tests. No visual or naming distinction between "thinking" and "answering" is made anywhere in `pico.tui`.

## [X] T002 Professional color palette

### Description

Redesign `PICO_THEME` in `src/pico/tui/theme.py` into a small, disciplined, professional palette — the kind a design agency would ship, not a default dark-mode preset. Keep the existing `Theme` dataclass contract (typed, immutable, semantic field names) but reconsider the actual values and, if needed, extend the field set to support the new panes from T003/T004 (e.g. distinct border/foreground pairs per pane kind, a dedicated input-bar accent, a dimmed/idle state).

Constraints:

* Every color is referenced through `Theme` fields; no bare hex/rich color strings inline in widget code.
* The palette must work as a single coherent system: consistent contrast, a clear accent hierarchy (one primary accent, not several competing bright colors), and legible muted/idle states.
* Keep it terminal-appropriate — verify it renders correctly in both a true-color and a 256-color terminal (Textual degrades automatically; confirm it degrades acceptably, do not add new machinery for this).

### Acceptance criteria

* `tests/tui/test_theme.py` is updated for any renamed/added fields and still confirms every semantic field is present and is a valid color string.
* Fully annotated, passes strict Pyright.

## [X] T003 Application chrome: header, layout, resize behaviour

### Description

In `src/pico/tui/app.py`, add the structural chrome around the conversation transcript:

* A slim header/title bar (e.g. app name, and a run-status indicator driven by `RunStarted`/`RunFinished`/`ErrorOccurred` — idle, running, error — using `Theme` colors, no new business logic).
* The transcript fills all remaining vertical space between the header and the input bar (T005), full window width, no fixed pixel/cell sizing that would clip on a smaller terminal.
* Verify the layout re-renders correctly on terminal resize (Textual's CSS-based layout should do this for free — confirm rather than hand-roll resize handling).

### Acceptance criteria

* `tests/tui/test_app.py` gains a test asserting the header reflects run state transitions (idle -> running -> finished/error) from published `BusEvent`s.
* No fixed-size layout that would break at common terminal sizes (test at minimum against an 80x24 and a wider/taller size using Textual's test harness size options).
* Fully annotated, passes strict Pyright.

## [X] T004 Redesigned conversation panes

### Description

Redesign the widgets in `src/pico/tui/widgets.py` (renaming per T001's decision) so each event kind reads as a visually distinct, clean pane in the transcript rather than a generic bordered box:

* Assistant text pane: minimal chrome, optimized for reading prose — this is the dominant content type and should feel the quietest/most prominent, not competing with tool output.
* Tool call pane: clearly shows the tool name, a compact rendering of streaming arguments while running, and a finished state that visually distinguishes success from error at a glance (color + a small glyph/marker, not color alone, for accessibility).
* Error pane/notification: distinct from both, unmistakably an error.

Keep the existing small, explicit widget API shape from `0004` (`append_delta`, `finish`) — this task changes visual design and, where T001 requires it, naming, not the interaction contract with `app.py`.

### Acceptance criteria

* `tests/tui/test_widgets.py` updated for any renames, still drives each widget through create -> delta -> delta -> finish via `App.run_test` and asserts rendered content and finished state.
* Success and error tool-call states are visually distinguishable by more than color alone.
* Fully annotated, passes strict Pyright.

## [X] T005 Bottom input bar

### Description

Add a persistent input bar pinned to the bottom of the screen, present for the entire lifetime of the app (not just when idle):

* A horizontal rule immediately above it and another immediately below it, full window width.
* A `>` prompt character on the left, followed by an editable text input region.
* It must not scroll away or get pushed off-screen by a long transcript — the transcript scrolls in the space above it; the bar itself is fixed.

Submission behaviour: wire the input to produce a typed, testable event (e.g. a Textual `Message` such as `UserInputSubmitted(text: str)`) posted on Enter, and clear the field after submission. Actually acting on submitted input (sending it into `pico.core`) is out of scope for this milestone — `0005-integration.md` and beyond own that wiring. This task only needs the bar to exist, render correctly, and emit the message; a no-op or trivial handler in `PicoApp` is sufficient here.

### Acceptance criteria

* The input bar renders at the bottom of the screen in `App.run_test` at multiple terminal sizes, with visible rules above and below and a `>` prompt.
* `tests/tui/test_widgets.py` or `tests/tui/test_app.py` covers: typing text and pressing Enter emits the submitted-text message and clears the field.
* Fully annotated, passes strict Pyright.

## [X] T006 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Manually confirm (no automated test required for this bullet) that `uv run python -m pico` — once `0005-integration.md` lands — would render the new chrome correctly; if `0005` has not landed yet when this task runs, verify visually via `PicoApp(Bus()).run()` in a throwaway script instead, and delete the script afterward.

### Acceptance criteria

* `make check` passes with no errors.
* `src/pico/tui/__init__.py` still re-exports the correct public surface for the (possibly renamed) types.
* Nothing outside `src/pico/tui/` needs to import from `pico.tui.app`, `pico.tui.theme`, `pico.tui.messages`, or `pico.tui.widgets` directly.

### Completion

Commit:
