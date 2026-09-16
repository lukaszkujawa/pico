# Session Lifecycle

Every conversation Pico has ever had lives under one hardcoded session id, `DEFAULT_SESSION_ID = "default"` (`src/pico/app.py`), inside whatever database `SESSION_DB_PATH` points at. `render_messages` (`src/pico/core/context.py`) replays that entire history — every user message, assistant reply, and tool call ever recorded under that id — into the LLM on every single turn, trimming only oversized tool-result content when the token budget is tight, never trimming whole prior conversations. Since nothing ever creates a new session id, this database just grows forever, and every question you ask is implicitly a continuation of everything you've ever asked before.

This was invisible on a throwaway host `pico.db`, but `0016-docker-run.md` bind-mounts a persistent `.docker_data/pico.db` specifically so history survives a container restart — which is exactly what surfaced the problem: ask an unrelated question after a prior, unrelated task (e.g. "review this GitHub repo"), and the model treats it as a continuation of the old task, because the entire old exchange is still sitting in its context with no boundary marking where one conversation ended and the next began. The same thing happens with plain `make run` on the host too — Docker didn't cause it, it just made a stale `pico.db` outlive the terminal session you expected to reset it.

This milestone adds an actual session lifecycle: a fresh, empty session by default, a way to explicitly resume a specific prior one, and a way to start over mid-run — without touching the event-sourcing model itself (`Session`/`SessionEvent`/the store schema from `0008-session-store.md` are correct as-is; this is purely about *which* session id a run uses and when).

## [X] T001 Fresh session id per process start, by default

### Description

In `src/pico/app.py`, replace the fixed `DEFAULT_SESSION_ID = "default"` used unconditionally in `run_pico` with a freshly generated id every time the process starts (e.g. a UUID4, or a readable timestamp-based id like the `RunLog` run-directory naming already does in `src/pico/debug/log.py` — pick whichever reads better in the session list added in T003). This is the new default behavior: starting Pico starts a new, empty conversation, matching what a user actually expects from launching a chat app.

Do not remove the underlying multi-session capability — `Session(conn, session_id)` and `Session.child(...)` (used by delegates) are unaffected; only what id `run_pico` picks by default changes.

### Acceptance criteria

* `tests/test_app.py` covers: two separate `run_pico` invocations against the same `SESSION_DB_PATH` produce two sessions with different ids, and `session.messages()` for one contains none of the other's turns.
* Existing tests that rely on a known/fixed session id (if any) are updated to not depend on the literal string `"default"`.
* Fully annotated, passes strict Pyright.

## [X] T002 `--resume [session_id]` CLI flag

### Description

In `src/pico/__init__.py`, add a `--resume` flag to the `argparse` parser, taking an optional value: `--resume` with no value resumes the most recently active session (most recent `created_at` across any event, per `SESSION_DB_PATH`); `--resume <id>` resumes that exact session id. Without `--resume` at all, behavior is T001's fresh-session default.

Add a small lookup in `src/pico/session/session.py` or `src/pico/session/store.py` (your call which fits better) to find the most-recently-active session id for a given connection — this is a new read-only query alongside the existing `events()`/`messages()` replay functions, not a change to how events are stored.

`main()` passes the resolved session id (or `None` for "generate a fresh one," per T001) through `run_pico`.

### Acceptance criteria

* `tests/test_main.py` covers: `--resume` alone resolves to the most-recently-active session id; `--resume abc123` resolves to exactly that id; no `--resume` flag results in a fresh id being generated (not reusing any existing one).
* `tests/session/test_session.py` or `test_store.py` covers the most-recently-active lookup: returns `None` (or raises — pick one, document it) on an empty database; returns the correct id when multiple sessions have events with different timestamps.
* Fully annotated, passes strict Pyright.

## [X] T003 In-TUI "new conversation" action

### Description

A user already mid-conversation shouldn't have to quit and relaunch to start fresh. Add a keybinding (e.g. `ctrl+n`, following the existing `escape` -> `action_cancel_run` pattern in `src/pico/tui/app.py`'s `BINDINGS`) that:

* Refuses to act while a run is in flight (mirror the existing `_run_in_flight` guard used by `action_cancel_run`) — starting a new session mid-turn would orphan the in-progress `LoopRunner`.
* Otherwise: generates a new session id (same mechanism as T001), swaps `PicoApp`'s `Session` for one pointing at the new id, and resets the visible conversation — clear mounted panes from `#conversation` (everything except a fresh `Splash`/`WaitingIndicator`), reset any per-session TUI state (the fact counter `_fact_count`, `_queued_user_panes`, pane dictionaries) the same way a fresh process start would have them.

This does not delete the old session's data — it's still in the database, resumable later via `--resume <id>` (T002). This action only changes which session the *current* process is writing to and reading context from.

### Acceptance criteria

* `tests/tui/test_app.py` covers: triggering the action mid-conversation clears visible panes and starts a session whose `messages()` is empty; the old session's events are untouched and still queryable under its original id; triggering it while `_run_in_flight` is true is a no-op (mirrors the existing cancel-while-idle test pattern).
* Fully annotated, passes strict Pyright.

## [X] T004 Surface the active session id somewhere visible

### Description

A user resuming a session, or wanting to `--resume` one later, needs to know which id they're in. Add the current session id somewhere low-key and always-visible in the TUI — the `Splash` widget's caption area is a reasonable fit (it already renders once at startup and stays in scrollback), or a short-lived toast/status line if that reads better; your call, but it must not require the user to dig through the database to find it.

### Acceptance criteria

* `tests/tui/test_app.py` or `test_widgets.py` covers: the rendered output contains the active session id after startup, and updates to the new id after the T003 new-conversation action fires.
* Fully annotated, passes strict Pyright.

## [X] T005 `README.md`

### Description

Document the new behavior: Pico starts a fresh conversation by default, `--resume` (bare or with an id) continues a prior one, and the in-TUI new-conversation keybinding (T003) starts over without losing the old history. Mention where the session id is shown (T004). Keep it proportional to the README's existing depth.

### Acceptance criteria

* A reader who hits this milestone's behavior for the first time (e.g. running `make run_in_docker` twice and getting two independent conversations, where they previously got one continuous one) can find out why and how to get the old behavior back (`--resume`) from the README alone.

## [X] T006 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Manually verify end-to-end: start Pico, have a conversation, quit; start Pico again against the same `SESSION_DB_PATH` — confirm the new run has no memory of the prior one; relaunch with `--resume` — confirm it does. Then, within one running instance, trigger the new-conversation action mid-session and confirm the visible transcript clears while the old session remains intact and resumable.

### Acceptance criteria

* `make check` passes with no errors, including the coverage floor in `pyproject.toml`.
* The manual walkthrough above behaves exactly as described, including against `make run_in_docker` specifically (the scenario that surfaced this milestone) — two consecutive `make run_in_docker` runs no longer bleed context into each other by default.

### Completion

Commit:
