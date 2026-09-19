# Tab Completes

Type `/q`, watch the menu narrow to a single `quit` row, and the natural next keystroke is Tab. Today Tab is not in `MENU_KEYS` and `ChatInput._on_key` falls through to `super()._on_key`, so Textual's default focus navigation claims it: the menu stays open, the text is unchanged, and focus jumps off the input entirely. The user is thrown out of the field they were typing in, by the key every shell, editor, and REPL has trained them to press. Tab is the completion key; it should complete.

## Design decisions

* **Tab completes the highlighted row while the menu is open.** It fills the input with the completion rather than running it — the difference from Enter, which accepts and executes. Completing `/q` yields `/quit`, leaving the user to press Enter; completing a command that takes an argument yields `/model ` with the cursor at the end, exactly as accepting `/model` does today. The `_accept` recomputation from the input's current text is reused, so Tab inherits the same race-free behaviour Enter has.
* **Tab with a closed menu keeps Textual's focus behaviour.** Only an open menu claims the key; nothing changes for the rest of the app, and `shift+tab` is never intercepted.
* **A single exact match still completes.** Pressing Tab when the input already reads `/quit` and the sole row is `quit` is a no-op on the text, not an error or a dismissal — the menu stays as it is and focus stays put.
* **The menu stays open after completing to a command that takes an argument,** so the argument rows appear as they do when Enter accepts `/model`; completing a terminal command like `/quit` hides it.
* **Scope guard.** No change to Enter, the arrow keys, Escape, or the completion source; no new bindings beyond Tab inside `ChatInput`; no common-prefix completion across multiple rows — Tab acts on the highlighted row only.

## [X] T001 Tab completes the highlighted row

### Description

Handle Tab in `ChatInput._on_key` while the command menu is displayed: fill the input from the recomputed completion instead of letting focus navigation take the key.

### Acceptance criteria

* With the menu open on `/q`, Tab sets the input to `/quit`, runs no command, and leaves focus on the input.
* Tab on a command awaiting an argument sets the input to `/<name> ` with the cursor at the end and the argument rows showing.
* Tab with the menu closed still moves focus as Textual does by default, and `shift+tab` is unaffected.
* Tab when the text already equals the only match leaves the text and focus unchanged.
* `make check` passes.
