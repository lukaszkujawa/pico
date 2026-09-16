# Debug Logging

Independent of the `0009`-`0014` declarative-loop arc — this is an observability feature that can land at any point in that sequence.

Today a run's prompts, responses, and loop behaviour are visible only by watching the TUI live; nothing is written to disk, so a bad run can't be inspected afterwards. This milestone adds an opt-in `--debug` flag that, when passed, writes everything sent to and received from the LLM, plus loop-level facts, to a per-run directory under `./logs/`.

## [X] T001 `--debug` CLI flag

### Description

In `src/pico/__init__.py`, parse `sys.argv` for a `--debug` flag using the standard library `argparse` (no new dependency). `main()` passes the resulting `debug: bool` through to `run_pico`.

In `src/pico/app.py`, `run_pico` gains a `debug: bool = False` parameter.

### Acceptance criteria

* `tests/test_main.py` covers: `--debug` present sets `debug=True` on the call to `run_pico`; absent defaults to `False`.
* Fully annotated, passes strict Pyright.

## [X] T002 `RunLog`: per-run log directory

### Description

In `src/pico/debug/log.py`, define `RunLog`:

* `RunLog.create(root: Path = Path("logs")) -> RunLog` — creates `root` if it doesn't exist, then creates a subdirectory named for the current local time down to the second in a readable, filesystem-safe format (`strftime("%Y-%m-%d_%H-%M-%S")`). If that exact directory already exists (two runs in the same second), append `-2`, `-3`, etc. until a free name is found — never overwrite an existing run's logs.
* `RunLog.write_prompt(messages: list[Message]) -> None` — writes `prompt-N.txt` in the run directory: an ISO 8601 local timestamp on the first line, then the full serialized prompt (every message, role and content, in the form sent to the LLM). `N` starts at 1 and increments by one on every call.
* `RunLog.write_response(text: str) -> None` — writes `resp-N.txt` the same way: timestamp first line, then the full raw response text. Shares the same counter sequence as prompts, so a prompt/response pair for one LLM call always has the same `N` (`write_prompt` then `write_response` for call 1 both write `*-1.txt`; call 2 writes `*-2.txt`).
* `RunLog.log(line: str) -> None` — appends a single timestamped line to `session.log` in the run directory (open-append, one line per call, never truncated mid-run).

Keep this a small, self-contained domain object: no import of `pico.core`, `pico.llm.client`, or `pico.tui` — it operates purely on strings, `Message` values (from `pico.llm.types`, already leaf-level), and the filesystem.

### Acceptance criteria

* `tests/debug/test_log.py` covers: `create` makes `logs/` if absent; the run subdirectory name matches the readable-timestamp format; two `create()` calls within the same second produce distinct, non-colliding directories; `write_prompt`/`write_response` share one incrementing counter starting at 1, each file's first line is a timestamp and the rest is the full content passed in; `log` appends timestamped lines to `session.log` without truncating prior lines.
* Fully annotated, passes strict Pyright.

## [X] T003 Wire `RunLog` into a run

### Description

`RunLog` must capture the exact prompt sent and exact response received without changing `OllamaClient` or `Run`. Add a small wrapping `LLMClient` in `src/pico/debug/log.py`, `LoggingLLMClient`, that takes a wrapped `LLMClient` and a `RunLog`: on `stream()`, it calls `run_log.write_prompt(messages)` before delegating, accumulates every `TextDelta`/`ThinkingDelta` chunk it forwards, and calls `run_log.write_response(...)` with the concatenated text once the wrapped stream is exhausted (in a `finally`, so a partial response from a cancelled or errored call is still flushed).

In `src/pico/app.py`, when `run_pico` is called with `debug=True`:

* Create a `RunLog` via `RunLog.create()`.
* Wrap the LLM client built by `_build_llm_client` in `LoggingLLMClient` before it reaches `Run`.
* Subscribe a background consumer to the `Bus` (same pattern as `PicoApp._consume_bus`) that writes one `RunLog.log(...)` line per `BusEvent` received, plus the config's `context_size` and vendor/model at startup. This is the catch-all for "everything agentic-loop-related not already captured by prompt/response files."

When `debug=False`, none of this runs — `run_pico`'s existing behavior is unchanged and no `logs/` directory is created.

### Acceptance criteria

* `tests/test_app.py` covers: `debug=True` creates a run directory under a temp `logs/` root (monkeypatch the root or cwd) containing at least one `prompt-1.txt`, `resp-1.txt`, and `session.log` after a turn completes; `debug=False` creates no `logs/` directory.
* Fully annotated, passes strict Pyright.

## [X] T004 Ignore `logs/`

### Description

Add `logs/` to `.gitignore`.

### Acceptance criteria

* `git check-ignore logs/anything` succeeds.

## [X] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green. Add `src/pico/debug/__init__.py` re-exporting `RunLog` and `LoggingLLMClient`.

### Acceptance criteria

* `make check` passes with no errors.
* Running `uv run python -m pico --debug` (against a reachable Ollama server) produces a `logs/<timestamp>/` directory with `prompt-1.txt`, `resp-1.txt`, and `session.log` after sending one message; running without `--debug` produces no `logs/` directory.

### Completion

Commit:
