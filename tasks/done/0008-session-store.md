# Session Store

Roadmap milestone 1 of 7 toward a declarative, SQL-backed agentic loop (see `0009`-`0014` for the rest of the arc). Full task detail below.

Today `Run` holds all state in a Python list (`self._messages`) that lives and dies with the process — nothing survives a restart, nothing is queryable, and nothing but the live object itself can inspect what happened during a run. This milestone replaces that with an event-sourced session: every meaningful thing that happens is appended as a row to a SQLite-backed log, and all higher-level state (conversation history, later: goals/facts/ledger, later: stuckness signals) is *derived* by reading that log, never held as separate mutable state that can drift from it.

This milestone only builds the store and the replay functions. It does not change `Run`, the bus, or the TUI — those integrate with it in `0009`. Treat this as a fully decoupled, independently testable component: no import of `pico.llm`, `pico.core.agent`, `pico.core.bus`, or `pico.tui` from this module.

## [X] T001 Add SQLite as the storage backend

### Description

Use the standard library `sqlite3` module — no new dependency to add. Confirm `uv run python -c "import sqlite3"` works in the project's managed environment (it's part of the Python 3.13 stdlib, so this is a sanity check, not a dependency addition).

In `src/pico/session/store.py`, define the schema and connection setup:

* A single `events` table: `id INTEGER PRIMARY KEY AUTOINCREMENT`, `session_id TEXT NOT NULL`, `seq INTEGER NOT NULL`, `kind TEXT NOT NULL`, `payload TEXT NOT NULL` (JSON-encoded), `created_at TEXT NOT NULL` (ISO 8601 UTC). `seq` is a per-session monotonic counter assigned by the store, not the caller, so ordering is guaranteed even under concurrent writers.
* An index on `(session_id, seq)`.
* A `connect(path: str | Path) -> sqlite3.Connection` helper that opens the DB (creating the schema if absent via `CREATE TABLE IF NOT EXISTS`) with `sqlite3.Row` row factory and WAL journal mode enabled (`PRAGMA journal_mode=WAL`) for safe concurrent read/write from separate threads.
* Support an in-memory DB (`:memory:`) for tests — the schema setup must work identically for both.

### Acceptance criteria

* `tests/session/test_store.py` covers: schema creation is idempotent (`connect` called twice on the same path doesn't error), the `events` table and index exist after connecting.
* Fully annotated, passes strict Pyright.

## [X] T002 Typed session events

### Description

In `src/pico/session/events.py`, define a small, frozen-dataclass event vocabulary for what a session records — deliberately narrower than `pico.core.events.BusEvent` (that's live UI-streaming events; these are durable facts about what happened, recorded once per meaningful occurrence, not per token delta):

* `UserMessageRecorded(content: str)`
* `AssistantMessageRecorded(content: str, thinking: str)`
* `ToolCallRecorded(name: str, arguments: Mapping[str, object], result: str, is_error: bool)`
* `SessionEvent = UserMessageRecorded | AssistantMessageRecorded | ToolCallRecorded`

Each carries only the data needed to reconstruct conversation state later — no ids, timestamps, or sequence numbers on the dataclasses themselves; the store (T003) owns those as storage metadata, keeping the domain types free of persistence concerns.

### Acceptance criteria

* `tests/session/test_events.py` covers construction of each variant and `SessionEvent` membership.
* Fully annotated, passes strict Pyright.

## [X] T003 `Session`: append and replay

### Description

In `src/pico/session/session.py`, define:

* `Session(conn: sqlite3.Connection, session_id: str)` — a thin wrapper, not a cache: it holds no in-memory copy of events between calls.
* `Session.append(event: SessionEvent) -> None` — serializes the event to JSON (`kind` = the dataclass's class name, `payload` = its fields), assigns the next `seq` for this `session_id` (via `MAX(seq)+1` in a transaction, or `SELECT ... FOR UPDATE`-equivalent locking appropriate to SQLite — a single connection with WAL + a lock around the read-then-insert is sufficient; do not over-engineer concurrency control beyond what a single-writer-per-session model needs), and inserts the row.
* `Session.events() -> Iterator[SessionEvent]` — reads all rows for `session_id` ordered by `seq`, deserializing each back into its typed dataclass. Unknown `kind` values raise a dedicated `UnknownEventKindError` rather than being silently skipped, since a silently-dropped event would corrupt replay.
* `Session.messages() -> list[pico.llm.types.Message]` — the first real "derive state by replaying the log" function: folds `events()` into the `Message` list shape `Run` currently builds by hand (`UserMessageRecorded` -> `Role.USER`, `AssistantMessageRecorded` -> `Role.ASSISTANT` with `content`, `ToolCallRecorded` -> a `Role.TOOL` message via `ToolResult`). This is deliberately the same shape `pico.llm.types.Message` already expects, so `0009` can swap `Run`'s message list for `session.messages()` directly with no shape changes elsewhere.

### Acceptance criteria

* `tests/session/test_session.py` covers: appending events and reading them back via `events()` preserves order and field values exactly; `messages()` on a sequence of user/assistant/tool events produces the same `Message` list shape a hand-built list would; two `Session` instances with different `session_id` on the same connection never see each other's events; an unknown stored `kind` raises `UnknownEventKindError` on replay.
* Fully annotated, passes strict Pyright.

## [X] T004 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Add `src/pico/session/__init__.py` re-exporting the public surface (`Session`, `SessionEvent` and its variants, `connect`).

### Acceptance criteria

* `make check` passes with no errors.
* `pico.session` has no import of `pico.llm.client`, `pico.core.agent`, `pico.core.bus`, or `pico.tui` — only `pico.llm.types` (for `Message`/`Role`/`ToolResult` in `Session.messages()`).

### Completion

Commit:
