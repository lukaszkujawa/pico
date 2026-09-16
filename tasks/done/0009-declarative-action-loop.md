# Declarative Action Loop

Roadmap milestone 2 of 7 (see `0008-session-store.md` for the full arc and rationale).

`Run._step` today hardcodes its control flow directly in Python: stream text, collect tool calls, run them, loop if any, stop otherwise. This milestone replaces that fixed shape with a declarative loop configuration — an ordered, data-described sequence of steps/conditions (a `LoopConfig` or similar) that a single generic runner executes, so that trying a different loop shape (different stop conditions, different step ordering, a max-steps cap, a different action set per `0010`) is a configuration change, not a rewrite of loop code.

Integrates `0008`'s `Session` as the loop's state: replaces `Run`'s hand-built `list[Message]` with `session.messages()`, and every step's outcome is recorded via `Session.append`, not just published to `Bus`. `Bus`/`BusEvent` stay as the live-streaming channel to the TUI (unchanged contract); `Session` becomes the durable record the loop itself reads back to decide what to do next.

"Declarative" here means concretely: a small typed `LoopConfig` dataclass holding an ordered tuple of step handlers plus a `max_steps` cap, executed by one generic `LoopRunner`. No rule engine, no DSL, no plugin registry — a step handler is just a function of `(LoopRunner) -> StepOutcome`, and `LoopConfig` is data describing which handlers run in what order. This is deliberately the smallest shape that makes "define a different loop" a matter of constructing a different `LoopConfig` rather than editing runner code.

## [X] T001 `StepOutcome` and `LoopConfig`

### Description

In `src/pico/core/loop.py`, define the data shape of a declarative loop:

* `StepOutcome = Literal["continue", "done", "cancelled"]` — same three outcomes `Run._step` returns today as a bare `str`, now typed.
* `LoopConfig`: a frozen dataclass with `steps: tuple[Step, ...]` and `max_steps: int | None = None`. `Step` is a type alias for `Callable[["LoopRunner"], StepOutcome]` (forward-referenced; `LoopRunner` is defined in T002 in the same module).
* A step handler receives the runner (so it can read `runner.session`, publish to `runner.bus`, call the LLM via `runner.llm`, etc.) and returns a `StepOutcome`. `LoopConfig.steps` are tried in order on each iteration; the first non-`"continue"` outcome from any step short-circuits the rest of that iteration's steps and is treated as the iteration's outcome. This mirrors `_step`'s current shape (stream -> maybe tool calls -> continue-or-done) but makes the sequence data instead of nested control flow.

Do not implement `LoopRunner` itself yet — this task is the config/outcome types only, so it can be unit tested in isolation before the runner exists.

### Acceptance criteria

* `tests/core/test_loop.py` (new) covers: `LoopConfig` construction with a tuple of dummy step functions; `StepOutcome` accepts exactly the three literal values (a type-checker-level guarantee, exercised by a test that constructs each value and passes it where `StepOutcome` is expected).
* Fully annotated, passes strict Pyright.

## [X] T002 `LoopRunner`: generic executor over `LoopConfig`

### Description

In `src/pico/core/loop.py`, add `LoopRunner`:

* Constructor: `LoopRunner(llm: LLMClient, tools: ToolRegistry, bus: Bus, session: Session, config: LoopConfig, cancel: threading.Event | None = None)`. No `messages: list[Message]` parameter — message history is `session.messages()`, read fresh each time a step needs it (no cached copy held by the runner, matching `0008`'s "no in-memory copy" principle for `Session` itself).
* `LoopRunner.execute() -> None` — publishes `RunStarted`, then repeatedly runs one iteration (each of `config.steps` in order, stopping at the first non-`"continue"` outcome) until an iteration returns `"done"` or `"cancelled"`, or `config.max_steps` iterations have run (hitting the cap behaves like `"done"` — publish `RunFinished` and stop; this is the hook `0012`/`0014` will later use for step budgets, but the cap itself belongs here since it's a loop-shape concern). Catches `LLMError` exactly as `Run.execute` does today (`ErrorOccurred` + `RunFinished(error=...)`, then return). Publishes `RunCancelled` on `"cancelled"`, `RunFinished()` on `"done"` or cap reached.
* A private `_new_id()` counter identical to `Run`'s, used by the streaming step handler for `Assistant*Started`/`Delta`/`Finished` stream ids.

Do not port the actual streaming/tool-calling logic yet — `execute()` and the iteration loop can be fully tested against dummy step functions (e.g. a step that always returns `"done"` immediately, one that counts calls and returns `"continue"` twice then `"done"`, one that raises `LLMError`).

### Acceptance criteria

* `tests/core/test_loop.py` covers: a single always-`"done"` step produces `RunStarted` + `RunFinished()`; a step sequence that returns `"continue"` N times then `"done"` runs exactly N+1 iterations; `"cancelled"` produces `RunStarted` + `RunCancelled()` and no `RunFinished`; a step that raises `LLMError` produces `RunStarted` + `ErrorOccurred` + `RunFinished(error=...)`; `max_steps` reached without a terminal outcome produces `RunFinished()` after exactly `max_steps` iterations; steps after the first non-`"continue"` step in one iteration are not called (use call-tracking dummy steps).
* Fully annotated, passes strict Pyright.

## [X] T003 Port streaming and tool-call steps, record to `Session`

### Description

Still in `src/pico/core/loop.py` (or a sibling `src/pico/core/steps.py` if `loop.py` grows unwieldy — prefer one file unless it clearly helps readability), implement the two step handlers that replace `Run._step`'s body, and the `LoopConfig` that wires them into the default agentic loop:

* `stream_step(runner: LoopRunner) -> StepOutcome` — the direct port of `Run._step`'s streaming loop: calls `runner.llm.stream(runner.session.messages(), runner.tools.specs())`, publishes the same `AssistantThinking*`/`AssistantText*`/`ToolCallArgumentsDelta` bus events as today, collects `text` and `tool_calls`. On completion (not cancelled): appends `AssistantMessageRecorded(content=text, thinking=<accumulated thinking text>)` to `runner.session`. Returns `"cancelled"` if `runner.cancel` was set mid-stream; otherwise returns `"continue"` if any tool calls were collected (so the next step handler runs), else `"done"`.
* `tool_call_step(runner: LoopRunner) -> StepOutcome` — only reached when `stream_step` returned `"continue"`. Needs the tool calls collected by `stream_step`; since steps are pure functions of `runner` with no shared return value between them, store the pending tool calls as transient state on the runner itself (e.g. `runner._pending_tool_calls: list[ToolCall]`, set by `stream_step`, consumed and cleared by `tool_call_step`) rather than widening `Step`'s signature — this keeps `Step`'s type simple while acknowledging steps in the *default* config are allowed to coordinate through the runner, same as `Run._step` and `_run_tool_call` share `self.messages` today. For each pending tool call: publish `ToolCallStarted`/`ToolCallFinished` exactly as `Run._run_tool_call` does today, then `runner.session.append(ToolCallRecorded(name=call.name, arguments=call.arguments, result=output, is_error=is_error))`. Always returns `"continue"` (there is always another stream step after tool results).
* `DEFAULT_LOOP_CONFIG = LoopConfig(steps=(stream_step, tool_call_step))` — the config equivalent to today's hardcoded `Run` behavior, with `max_steps=None` (unbounded, matching current behavior; a bounded variant is `0012`'s concern, not this milestone's).

`AssistantMessageRecorded.thinking` needs a value — accumulate thinking text the same way `text` is accumulated in the existing `_step`, defaulting to `""` when there was no thinking stream.

### Acceptance criteria

* `tests/core/test_loop.py` covers the full `DEFAULT_LOOP_CONFIG` behavior end-to-end with a `Session` backed by `:memory:`, replacing (not duplicating) the scenarios in `tests/core/test_agent.py`: plain text run, thinking+text with shared stream ids across two turns, single tool call round trip, tool call arguments delta forwarded, unknown tool surfaced as error, LLM error surfaced, cancel-before-streaming, cancel-mid-stream, cancelled turn publishes no `RunFinished`. Additionally assert `session.events()` after each scenario contains the expected `UserMessageRecorded`/`AssistantMessageRecorded`/`ToolCallRecorded` sequence (this is the new behavior `test_agent.py` never covered).
* Fully annotated, passes strict Pyright.

## [X] T004 Retire `Run`, wire `app.py` to `LoopRunner` + real `Session`

### Description

This is the task that makes the milestone actually load-bearing rather than a parallel unused code path:

* Delete `src/pico/core/agent.py` and `tests/core/test_agent.py` — `LoopRunner` + `DEFAULT_LOOP_CONFIG` fully replace `Run`; no hand-rolled `_step` control flow may remain reachable from anywhere in `src/pico`.
* `src/pico/config.py`: add a required `session_path: str` field to `Config`, loaded via `_require("SESSION_DB_PATH")` in `load_config()`. Update `.env.example` with `SESSION_DB_PATH=pico.db`. Add `*.db` and `*.db-*` (WAL/SHM sidecar files) to `.gitignore`.
* `src/pico/app.py`: `_turn_loop` opens the session store once via `pico.session.connect(config.session_path)`, creates a single `Session` (a fixed `session_id`, e.g. `"default"` — Pico has no multi-session concept yet, so a constant is the simplest correct choice; do not build session management UI/selection, that's out of scope), and on each queued message: `session.append(UserMessageRecorded(content=text))`, then constructs a fresh `LoopRunner(llm, tools, bus, session, DEFAULT_LOOP_CONFIG, cancel)` and calls `.execute()`. `run_pico` is otherwise unchanged (still builds `llm`, `tools`, `bus`, starts the core thread, runs `PicoApp`).
* Update `tests/test_app.py`'s `_config()` helper to include `session_path` (point at a temp file per test, e.g. via `tmp_path` fixture, so tests don't share or pollute a real `pico.db`); update any assertions that inspected `RecordingClient.seen_messages` shapes if `session.messages()`'s reconstructed `Message` list differs in any field from the old hand-built list (it shouldn't, per `0008` T003's design, but confirm).

### Acceptance criteria

* `grep -rn "core.agent\|core/agent" src tests` returns nothing.
* `tests/test_app.py` passes with `LoopRunner`/`Session` wired in, using per-test temp DB files.
* Fully annotated, passes strict Pyright.

## [X] T005 End-to-end TUI verification

### Description

Confirm a full turn — user message in, assistant response out, session persisted — works through the new loop before this milestone is marked done:

* Add a test in `tests/test_app.py` (or extend an existing one) that runs `run_pico` with a patched `OllamaClient` (following the existing `_patch_ollama_client`/`RecordingClient` pattern), drives one full turn through a real (temp-file-backed, not `:memory:`) `Session`, and after the turn asserts the session file on disk contains the expected `UserMessageRecorded` + `AssistantMessageRecorded` events by opening a fresh `pico.session.connect()`/`Session` against the same path and reading `.events()` back — proving persistence survived the run, not just in-process state.
* Manually run `uv run python -m pico` against a real or stubbed Ollama endpoint if available in this environment to sanity-check the TUI still starts and accepts input; if no live LLM endpoint is reachable in this environment, rely on the automated end-to-end test above as the verification and note that manual TUI verification was skipped for that reason.

### Acceptance criteria

* The new persistence-round-trip test passes.
* No task in this milestone is marked done until this test exists and passes.

## [X] T006 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Update `src/pico/core/__init__.py` (if it re-exports `Run` or similar) to reflect the new public surface (`LoopRunner`, `LoopConfig`, `StepOutcome`, `DEFAULT_LOOP_CONFIG`, `stream_step`, `tool_call_step`).

### Acceptance criteria

* `make check` passes with no errors.
* `uv run python -c "import pico.core.agent"` fails with `ModuleNotFoundError` (module no longer exists).

### Completion

Commit:
