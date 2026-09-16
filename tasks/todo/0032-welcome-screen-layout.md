# Welcome Screen Layout

The `Splash` widget (`src/pico/tui/widgets.py`) currently stacks everything in one column: the boxed prompt glyph on top, then the `PICO` label centered under it, then the tagline, then the session line. Redesign it into a two-column layout: the ASCII art sits on the left, and `PICO`, the tagline, the session line, and the local directory line stack to its right, vertically centered against the art's height.

## Design decisions

* **Two columns, art on the left.** The existing box-and-cursor glyph (`LOGO_TOP`/`LOGO_PROMPT`/`LOGO_MID`/`LOGO_BOTTOM`) stays exactly as it renders today, just moved to occupy the left column instead of being centered above the text block.
* **`PICO` sits beside the art, in bold white, not under it.** "Bold white" is `theme.text` (the theme's primary foreground) rendered bold — the same token already used for other bold-emphasis text elsewhere in the TUI, not a new hardcoded color.
* **Tagline directly under `PICO`, in grey.** `TAGLINE` ("Small model. Real agency.") keeps its existing string constant; grey is `theme.muted_text`, matching how the tagline and session line already render today. Drop the current `italic` style — the redesign calls for plain grey, not italic grey.
* **Session line under the tagline**, own row, `theme.muted_text` — same content and condition as today (`session {id}`, omitted entirely when there is no session id).
* **Local directory line under the session line.** New line, not present today: renders the current working directory the app was launched from, e.g. `~/projects/pico` when under the home directory (use `~` shorthand the way shells do), styled `theme.muted_text` like the other metadata lines. Threaded into `Splash` the same way `session_id` is: a constructor argument, wired from `app.py` where `Splash(...)` is constructed.
* **Right column is vertically centered on the art's height**, not top-aligned — the art block is 4 lines tall (`LOGO_TOP`/`LOGO_PROMPT`/`LOGO_MID`/`LOGO_BOTTOM`); the four right-column lines (`PICO`, tagline, session, directory) are exactly 4 lines when a session id is present. When there is no session id, the right column is 3 lines and should still center against the 4-line art rather than top-align.
* **No change to the blinking cursor behavior** inside the art (`LOGO_CURSOR`, the `blink` style on `>_`) — purely a layout change, not a content or animation change.
* **`LOGO_INDENT`/`LABEL_INDENT` become unnecessary** once the label is no longer centered under the art — remove them rather than leaving dead code, and recompute any layout spacing needed for the new two-column composition directly in `Splash.render()` or via Textual layout (see T001 for the choice).

## [ ] T001 Two-column render

### Description

Rewrite `Splash.render()` in `src/pico/tui/widgets.py` to lay the art out on the left and the `PICO`/tagline/session/directory stack on the right, vertically centered against the art. Decide and implement whichever mechanism keeps this simplest given `Splash` is a `Static` rendering a single `Text` — either pad each right-column line with enough leading spaces to sit beside the corresponding art line within one joined `Text`, or restructure `Splash` as a container composing two child widgets side by side if that ends up cleaner. Prefer the smaller change; do not introduce a new widget hierarchy unless padding a single `Text` genuinely cannot express the layout.

Remove `LOGO_INDENT` and `LABEL_INDENT`. Add a `local_directory` (or equivalent) parameter to `Splash.__init__`, defaulting so existing callers that don't pass it still work, and wire the actual value from `app.py` at the `Splash(...)` construction site using the process's current working directory with `~` substituted for the home directory when applicable.

### Acceptance criteria

* `tests/tui/test_widgets.py` covers: the art (`LOGO_TOP` etc.) appears on the left of the same rendered lines that contain `PICO`, the tagline, and (when present) the session line — i.e. they occupy the same row indices, not stacked sequentially; `PICO` renders with a bold style using `theme.text`; the tagline renders in `theme.muted_text` with no italic style; the local directory line renders using `theme.muted_text` and shows the expected path (including the `~` substitution case); the existing session-id-present and session-id-absent behaviors from `test_splash_*` continue to pass with the new layout logic swapped in.
* `tests/tui/test_app.py`'s existing `Splash` assertions (`session-1`, `session-2`, absent-session count) continue to pass unmodified in intent, updated only for whatever `Splash(...)` constructor signature results from this task.
* Fully annotated, passes strict Pyright.

## [ ] T002 Verify and finalize

### Description

Run `make check` and fix everything until green.

### Acceptance criteria

* `make check` passes, including the coverage floor.
* `grep -n "LOGO_INDENT\|LABEL_INDENT" src/pico/tui/widgets.py` returns nothing.

### Completion

Commit:
