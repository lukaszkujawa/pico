# Responsive Command Menu

The TUI freezes solid, and the trigger is a synchronous HTTP request on the UI event loop. `on_text_area_changed` in `tui/app.py` fires on every keystroke and calls `commands.complete`; once the input reads `/model `, `complete` invokes `ModelSwitch.available`, which calls `client.models()` — a blocking httpx GET with a ten-second timeout — inside a Textual message handler. While it runs, nothing repaints and no input is processed, and every further keystroke of the argument queues another fetch behind the first. A cross-pump race routes `/q` into this same hole: Enter is handled on `ChatInput`'s message pump while the menu is filtered on the app's pump, so a fast `/q<Enter>` accepts the stale menu from `/` — whose first row is `model`, since `COMMANDS` lists it first — and the input becomes `/model `, firing the blocking fetch the user never asked for. The symptom is a frozen UI with `/model ` sitting in the input box after typing `/q`. Two independent fixes: Enter must act on what was actually typed, and the model list must be fetched off the event loop, once, not per keystroke.

## Design decisions

* **`commands.complete` becomes pure.** It no longer calls anything that can block: instead of a `ModelSwitch`, it receives the already-fetched model names (the existing `Options`, or none while a fetch is pending) and only filters and formats. All fetching moves to the app layer; no code path reachable from a message handler performs network I/O.
* **One fetch per menu, in a Textual worker.** When argument completion for `/model` first opens, the app starts a worker (`run_worker` with `thread=True`, `exclusive=True` in a dedicated group) that calls `available()` and posts the result back. Until it lands, the menu shows a single unselectable "fetching models…" row; when it lands, the menu re-renders with the real rows, and subsequent keystrokes filter the cached names locally — the way `_command_rows` already filters command names. The cache lives while the menu is open and is dropped when it hides, so reopening fetches fresh; no TTL machinery. A fetch error renders through the existing `Completion.error` path.
* **Enter recomputes before accepting.** On Enter, `ChatInput` does not trust the menu widget's possibly-stale display; the accepted command is recomputed from the input's current text. A stale highlight can then never turn `/q` into `/model`.
* **`quit` moves before `model` in `COMMANDS`.** Belt and braces: if any staleness survives, the default highlighted row is the harmless one.
* **Scope guard.** No change to the LLM clients, their timeouts, or `models()` itself; no persistent or cross-session caching of model names; no change to how `switch_to` builds clients. The uncancellable stalled-stream problem (`read=None` plus cancel checked only per stream event) is real but separate — it is not this milestone.

## [ ] T001 Race-free accept

### Description

Recompute the completion from the input's current text when Enter accepts, and reorder `COMMANDS` so `quit` precedes `model`.

### Acceptance criteria

* With the menu displaying stale rows for a previous input state, Enter on an input of `/q` runs `quit`, never `model`, covered by a test that exercises the stale-menu state directly.
* Accepting `/model` from a menu genuinely matching the current text still inserts `/model ` and awaits the argument.
* `make check` passes.

## [ ] T002 Pure completion over provided names

### Description

Change `commands.complete` to take fetched model names instead of a `ModelSwitch`, filtering and formatting only.

### Acceptance criteria

* `complete` performs no calls beyond filtering its inputs; the `ModelSwitch` protocol is no longer imported by `commands`.
* Filtering, current-model marking, and the error rendering behave as today given the same names.
* `make check` passes.

## [ ] T003 Worker-fetched model list

### Description

Fetch the model list once per menu opening in an exclusive threaded worker, render a pending row until it arrives, filter the cached names on later keystrokes, and drop the cache when the menu hides.

### Acceptance criteria

* Opening `/model ` completion triggers exactly one fetch regardless of how many argument characters follow, covered by a counting fake.
* Keystrokes while the fetch is pending leave the UI responsive and show the pending row; the resolved names appear without further input, and a fetch error shows through the error row.
* Closing and reopening the menu fetches again; the superseded worker is cancelled rather than queued when completion reopens quickly.
* No test or production path invokes network I/O from a message handler.
* `make check` passes.
