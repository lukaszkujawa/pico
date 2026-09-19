# Packaging Hygiene

Two leftovers from `uv init` still sit in the project's front door. `pyproject.toml` ships `description = "Add your description here"` — the placeholder would appear verbatim on any index listing or `pip show`. And the console script points at `pico:main`, because the CLI — argument parsing, config loading, session resolution — lives in `src/pico/__init__.py`. That makes every `import pico` execute the CLI module and, through `pico.app`, drag in Textual, the LLM clients, and the session machinery whether the importer wanted a TUI or one dataclass. A package's `__init__` should say what the package is, not be the application.

## Design decisions

* **The CLI moves to `pico/cli.py`.** `main`, `_resolve_session_id`, and `RESUME_LATEST` move verbatim; `__init__.py` keeps at most lightweight re-exports that pull in no application modules, and may simply be empty. `__main__.py` and the `[project.scripts]` entry both point at `pico.cli:main`, so `uv run python -m pico` and the installed `pico` script behave exactly as today.
* **The description states what Pico is in one line,** taken from the README's opening: an agentic harness for local and smaller language models. No other metadata changes — version, authors, and readme stay as they are.
* **Scope guard.** No behaviour changes, no new flags, no restructuring beyond the one module move, no touching `run_pico` or `load_config` themselves. `tach.toml` is adjusted only as far as the moved module requires.

## [ ] T001 CLI out of the package init

### Description

Move the CLI into `pico/cli.py`, empty `__init__.py` of application code, and point `__main__.py` and the console script at `pico.cli:main`.

### Acceptance criteria

* `import pico` imports no application modules — no Textual, httpx, or `pico.app` in `sys.modules` afterwards, covered by a test run in a subprocess.
* `uv run python -m pico --help` and the `pico` console script both reach the same `main` and behave as today; `tests/test_main.py` follows the move.
* `[project.scripts]` reads `pico = "pico.cli:main"`.
* `make check` passes.

## [ ] T002 Real project description

### Description

Replace the placeholder `description` with the one-line statement of what Pico is.

### Acceptance criteria

* `pyproject.toml` no longer contains "Add your description here"; the description matches the README's characterisation.
* `make check` passes.
