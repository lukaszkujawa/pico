# Slash Commands

The TUI has no way to talk to the app instead of the agent: quitting means Ctrl-C, and changing the model means editing `.env` and restarting. This milestone adds slash commands to the chat input — type `/` and an autocomplete menu appears below the input, narrowing and preselecting as you type; enter runs the selection. Two commands to start: `/quit` exits the app, and `/model` lists the models the configured vendor actually serves and switches to the chosen one for subsequent runs.

Slash commands are a TUI concern. A message starting with `/` is intercepted in the TUI layer and never reaches the core loop or the session transcript; the core does not know slash commands exist, and the `pico.tui → pico.core` dependency direction is untouched.

## Design decisions

* **A tiny command registry, data not framework.** A `SlashCommand` dataclass — name, description, how to complete its argument, what to do when run — and a tuple of the two commands. No plugin machinery, no dynamic discovery; adding a third command later is adding one entry.
* **The menu is one widget with one contract.** A `CommandMenu` mounted below the input bar, hidden until the input's first character is `/`. It shows the commands whose names start with the typed prefix, description alongside, with the first match preselected; up/down move the selection, typing narrows it, enter accepts the preselected entry, escape dismisses and leaves the typed text. Enter on a complete command with no argument stage runs it; enter on `/model` advances to its argument stage.
* **`/model` is a two-stage completion.** Accepting `/model` swaps the menu's options for the vendor's available models — fetched live, not hardcoded — with the same narrowing and preselection over model names. Accepting a model performs the switch and clears the input. Typing the whole thing by hand (`/model qwen3.6:35b` + enter) works identically; the menu is an accelerator, not a gate.
* **Model discovery extends the LLM protocol.** `LLMClient` grows a `models() -> list[str]` method; `OllamaClient` implements it via the vendor's model listing endpoint (`GET /api/tags`), reusing its existing base URL, auth, and error wrapping (failures surface as `LLMError`, shown in the menu as an error row, never a crash). One protocol method, implemented by the one client — the seam future vendors already need.
* **Switching swaps a handle, following the established pattern.** `run_pico` currently passes the client into `_turn_loop` by value; like `SessionHandle` and `CancelHandle`, an `LLMHandle` now owns the current client, `_turn_loop` reads it at the start of each run, and `/model` builds a replacement client from the existing `Config` with only the model name changed. A switch mid-run affects the next run, never the streaming one.
* **Debug logging survives a swap.** When `--debug` wraps the client in `LoggingLLMClient`, the handle owns the wrapping: it is constructed with the wrap step and re-applies it to every replacement, so a model switch cannot silently drop the run log.
* **Feedback goes to the conversation, not a toast.** A successful switch mounts a small system-style line in the conversation ("model → qwen3.6:35b"); `/quit` needs none. Unknown commands (`/foo` + enter) show the same style line naming the known commands and leave the input intact.
* **The session is untouched by commands.** No command appends session events; `/model` changes which client future runs use, nothing about the transcript. Restarting the app returns to the `.env` model — persisting the choice is deliberately out of scope.

## [X] T001 Command registry and interception

### Description

Add the `SlashCommand` registry to the TUI and intercept slash input: when submitted text starts with `/`, parse it in the TUI layer instead of posting it to the core. Implement `/quit` (exit the app) and the unknown-command feedback line. `/model <name>` typed in full performs the switch via the `LLMHandle` introduced here: add `models()` to the `LLMClient` protocol, implement it on `OllamaClient` against the tags endpoint with `LLMError` wrapping, add the handle with debug-wrap preservation, and make `_turn_loop` read the client from the handle each run.

### Acceptance criteria

* `/quit` exits cleanly; `/foo` shows the feedback line naming known commands; neither appends anything to the session or starts a run.
* `/model <available-name>` switches the client; the next run streams from the new model while the current run, if any, finishes on the old one; the conversation shows the confirmation line.
* With `--debug`, runs after a switch still produce prompt/resp files in the same run log.
* `OllamaClient.models()` returns the served model names and raises `LLMError` on connection failure; a failed fetch shows an error line instead of crashing.
* Plain messages, including ones containing `/` beyond position zero, reach the core exactly as before.
* `make check` passes.

## [X] T002 Autocomplete menu

### Description

Add the `CommandMenu` widget below the input bar with the narrowing-and-preselection contract from the design decisions, wired to the registry: visible only while the input starts with `/`, first match preselected, up/down to move, enter to accept, escape to dismiss. Accepting `/quit` runs it; accepting `/model` enters the argument stage, which shows the live model list (current model marked) with the same narrowing behaviour, and accepting a model performs the switch. The menu never steals focus from the input; all keys land in `ChatInput` and are forwarded.

### Acceptance criteria

* Typing `/` shows both commands with descriptions; typing `/q` narrows to `/quit` preselected; enter quits.
* In the `/model` stage, typing narrows the fetched model names and preselects the best match; enter switches and clears the input; the current model is visibly marked.
* Escape dismisses the menu leaving the typed text; deleting back past `/` hides it; a non-`/` first character never shows it.
* Arrow keys move the selection without moving the input cursor while the menu is open, and behave exactly as before when it is closed.
* A model-list fetch failure renders an error row in the menu and the input keeps working.
* `make check` passes.

## [X] T003 Final soundness sweep

### Description

Run `make check` over the finished milestone and fix everything it surfaces — lint, format, strict typing, dead code, `tach` boundaries, tests, build. Read the new TUI modules once more purely for readability: the registry should read as data, the menu as one small widget with an obvious keyboard contract, and the handle swap should look like its `SessionHandle` sibling.

### Acceptance criteria

* `make check` passes clean from a fresh run.
* No orphaned symbols or unused styles remain; `tach check` confirms the dependency direction is unchanged.
