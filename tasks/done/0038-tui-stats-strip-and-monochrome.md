# TUI Stats Strip and Monochrome Theme

The input line sits at the very bottom and the only run feedback is the status line inside the conversation. Move the input up and put a compact stats strip beneath it — btop's information density, pico's minimalism — and flatten the palette to monochrome so the few remaining colors carry meaning.

## Design decisions

* **Layout**: the footer becomes input row first, stats strip under it. Conversation above, unchanged. The stats strip is always visible, one to two rows, box-drawn like the splash logo.
* **Stats strip contents**, all derived from data the app already sees or is handed at construction:
  * **Context meter**: last `GenerationCompleted.prompt_tokens` against `context_size`, as a filled bar plus `used/total` numbers. `context_size` is passed into `PicoApp` (it currently does not receive it).
  * **Request counter**: LLM generations this session (count of `GenerationCompleted`).
  * **Completion tokens**: the existing token counter, relocated here.
  * The existing spinner and elapsed timer move into the strip; the in-conversation status line disappears.
* **Stats reset** on new session (ctrl+n), and the context meter shows an empty bar until the first generation completes.
* **Monochrome theme**: `tui/theme.py` collapses text, borders, backgrounds, user/assistant/thinking/tool styling to greyscale steps. Color survives only where it is an icon or identity: the success `✓`, error `✗`, spinner frames, answer markers (`●`/`↺`), and the splash logo's prompt `>` and blinking cursor. The context meter bar may use one accent that shifts to the error color when usage crosses the budget.
* **No new dependencies**; Rich/Textual primitives only. Bus protocol unchanged — the strip consumes existing events.

## [X] T001 Stats strip under a raised input line

### Description

Restructure the footer (input above, stats strip below), implement the strip widget fed by existing bus messages plus `context_size`, remove the in-conversation status line, and relocate spinner/elapsed/tokens into the strip.

### Acceptance criteria

* The strip shows context meter (bar and `used/total`), request count, completion tokens, spinner, and elapsed; values update as generations complete and reset on ctrl+n.
* Input focus, submit, queueing, cancel, and new-session behaviour are unchanged.
* `make check` passes.

## [X] T002 Monochrome theme

### Description

Rework `tui/theme.py` and widget styles to the greyscale palette per the design decisions, keeping color only for the named icons, markers, and the splash logo accents.

### Acceptance criteria

* No widget styles text, borders, or backgrounds with a hue; the only colored elements are the listed glyphs, markers, logo accents, and the context meter's over-budget state.
* `make check` passes.
