# Integration

Wire the LLM abstraction (`0002-llm-abstraction.md`), application core (`0003-application-core.md`), and terminal UI (`0004-terminal-ui.md`) together behind the `pico` entry point. This is the first milestone where the three components run together as one program.

## [ ] T001 Environment configuration

### Description

Pico is configured via a `.env` file at the repository root (already present locally, gitignored). It currently defines:

```
LLM_VENDER=ollama
LLM_BASE_URL=http://192.168.1.134:11434/v1
LLM_MODEL=qwen3.8:latest
LLM_API_KEY=
LLM_CONTEXT_SIZE=128000
```

Add `python-dotenv` as a runtime dependency with `uv add python-dotenv`.

In `src/pico/config.py`, define a `Config` dataclass (`vendor`, `base_url`, `model`, `api_key: str | None`, `context_size: int`) and a `load_config() -> Config` that loads `.env` (via `dotenv`) and reads the `LLM_*` environment variables, raising a dedicated `pico.config.ConfigError` with a clear message for any missing required variable (`LLM_BASE_URL`, `LLM_MODEL`) or a non-integer `LLM_CONTEXT_SIZE`. `LLM_API_KEY` is optional; an empty string is treated as `None`. Note the existing `.env` key is `LLM_VENDER` (not `LLM_VENDOR`) — read that exact name rather than "fixing" the typo, since it is the real deployed key.

Also add `.env.example` at the repository root, mirroring the real keys with placeholder values and no secrets, so the project is runnable by anyone who copies it to `.env`.

### Acceptance criteria

* `load_config()` returns a populated `Config` when all required variables are set, and raises `ConfigError` naming the missing/invalid variable otherwise.
* `tests/test_config.py` covers: full config loads correctly, each required variable missing raises `ConfigError`, empty `LLM_API_KEY` becomes `None`, non-integer `LLM_CONTEXT_SIZE` raises `ConfigError`. Tests must not depend on the real `.env` file — set environment variables explicitly per test and clear them afterward.
* `.env.example` exists and documents every key with a placeholder value.
* Fully annotated, passes strict Pyright.

## [ ] T002 Application wiring

### Description

In `src/pico/app.py`, implement the top-level wiring `run_pico(config: Config) -> None`:

* Construct a `Bus`, an `OllamaClient` using `config.base_url`, `config.model`, and `config.api_key`, and a `ToolRegistry`. `config.vendor` selects the client implementation; only `"ollama"` is supported for now, and any other value raises a clear error rather than silently defaulting.
* Run application core's agentic loop (`Run`) on a background thread, publishing to the `Bus`.
* Run `PicoApp` (from `pico.tui`) on the main thread, consuming the same `Bus`.
* Ensure clean shutdown: when the TUI exits, the core thread is signalled to stop and joined; when the core run finishes, the TUI reflects `RunFinished` rather than hanging.

This module is the only place in the codebase allowed to import from all three of `pico.llm`, `pico.core`, and `pico.tui` together.

### Acceptance criteria

* `run_pico` starts core on a background thread and the TUI on the main thread, sharing one `Bus`.
* `tests/test_app.py` covers shutdown behaviour: stopping the TUI does not leave the core thread running, using a fake `LLMClient` so no real Ollama server is required.
* An unsupported `config.vendor` raises a clear error before any thread is started.
* Fully annotated, passes strict Pyright.

## [ ] T003 Replace placeholder entry point

### Description

Replace the placeholder `main()` in `src/pico/__init__.py` with one that calls `load_config()` and passes the result to `run_pico`. On `ConfigError`, print a clear message to stderr and exit with a non-zero status rather than showing a traceback. Remove the old "Hello from pico!" placeholder and its test in `tests/test_main.py`, replacing it with a test appropriate to the new `main()`.

### Acceptance criteria

* `uv run python -m pico` loads `.env` and launches the TUI against the configured Ollama server.
* `tests/test_main.py` covers: `main()` calls `run_pico` with the loaded config (using a monkeypatch/fake, no real TUI launch), and a `ConfigError` results in a clean non-zero exit rather than a traceback.
* Fully annotated, passes strict Pyright.

## [ ] T004 Manual smoke test

### Description

With the `.env` file's configured Ollama server reachable, run `uv run python -m pico` and confirm interactively: the TUI renders, a thinking box appears and streams tokens, and the app exits cleanly on quit.

### Acceptance criteria

* Smoke test performed and confirmed working; note the model used in the commit message.
* If the configured Ollama server is not reachable from this environment, this task cannot be completed — stop and leave it unchecked rather than marking it done without verification.

## [ ] T005 Verify and finalize

### Description

Run `make check` and fix everything until it is green.

### Acceptance criteria

* `make check` passes with no errors.
* `README.md` gains a short usage section: copy `.env.example` to `.env`, fill in the values, run `uv run python -m pico`.

### Completion

Commit:
