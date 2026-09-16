# Run Pico in Docker

Today `make run` requires a local `uv` install, a matching Python, and whatever system packages the terminal/TUI needs on the host. This milestone adds `make run_in_docker`, a containerized equivalent that a user can invoke without any local Python setup at all — same interactive TUI, same `.env` configuration, same persisted session, just running inside a container. It must build the image (rebuilding only when something relevant changed), run it attached to the current terminal so Textual's interactive input works exactly as it does natively, and leave nothing extra behind afterward — no dangling containers, no orphaned images accumulating on every invocation.

Out of scope: containerizing `make code`, `make test`, or any other target; publishing the image anywhere; supporting a bundled/containerized Ollama (the container reaches an LLM the same way the host process does today, via `LLM_BASE_URL` in `.env`, which may point at the host's own Ollama).

## [X] T001 `Dockerfile`

### Description

Add a `Dockerfile` at the repo root that builds a runnable Pico image:

* Base on an official `python:3.13-slim` image (matching `requires-python = ">=3.13"` in `pyproject.toml`).
* Install `uv` (pinned version, via the official `uv` install method appropriate for a slim Debian base — e.g. copying the static binary from `ghcr.io/astral-sh/uv`'s image rather than piping a script through shell, so the build has no non-reproducible network-script step).
* Copy only `pyproject.toml` and `uv.lock` first and run `uv sync --frozen --no-dev` (or equivalent) before copying the rest of the source, so dependency layers cache correctly and rebuilds are fast when only application code changes.
* Copy `src/` in afterward.
* Set the container's entrypoint/command to run the app the same way `make run` does (`uv run python -m pico`, or the installed `pico` console script — pick one and keep it consistent with T003).
* No `.env` file is baked into the image — configuration is supplied at `docker run` time (T002/T003).

### Acceptance criteria

* `docker build -t pico .` succeeds from a clean checkout.
* The resulting image contains no copy of a real `.env` (verify with `docker run --rm pico find / -maxdepth 2 -name .env` or equivalent — only `.dockerignore`-excluded paths).
* Changing a file under `src/` and rebuilding does not re-run `uv sync` (confirm via `docker build` layer cache output showing the dependency-install layer as cached).

## [X] T002 `.dockerignore`

### Description

Add a `.dockerignore` at the repo root excluding at minimum: `.venv/`, `.git/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.hypothesis/`, `.mypy_cache/`, `.pyright/`, `.coverage*`, `htmlcov/`, `logs/`, `*.db`, `*.db-*`, `.env`, `.env.*` (but not `.env.example`), `tasks/`, `bin/`. Mirror `.gitignore` where the intent overlaps (build artifacts, caches, local state) rather than duplicating it blindly — anything that must never end up inside the image layer, not just things git shouldn't track.

### Acceptance criteria

* `docker build` context size (reported in the build output) drops significantly versus building without this file present (spot-check, no fixed threshold).
* None of the excluded paths appear in the built image's filesystem.

## [X] T003 `bin/run_in_docker.sh`

### Description

Add `bin/run_in_docker.sh`, following the same shell conventions as `bin/code.sh` / `bin/code_loop.sh` (`#!/usr/bin/env bash`, `set -uo pipefail`, resolve `ROOT_DIR` from `BASH_SOURCE`). It must:

* Build the image from the `Dockerfile` (T001), tagged with a fixed, predictable name (e.g. `pico:local`) so repeated runs reuse and update one tag rather than accumulating new ones.
* Run the image with `docker run --rm -it` (interactive TTY, auto-removed on exit — no stopped container left behind) plus:
  * `--env-file .env` so the container reads the same configuration `make run` does. Fail early with a clear message if `.env` is missing, the same way `load_config()` fails today for a missing variable — don't let docker itself produce the error.
  * A bind mount (or named volume — pick one, see acceptance criteria) for the session database so conversation history in `SESSION_DB_PATH` persists across container runs exactly as it does between two `make run` invocations on the host.
* Forward the container's exit code as the script's own exit code, so shell scripting around `make run_in_docker` behaves identically to `make run`.

### Acceptance criteria

* Running the script twice in a row against the same `.env` shows the second run continuing the first run's session (same conversation history), proving persistence survived the container's removal.
* Running it with no `.env` present fails fast with a message naming the missing file, before any `docker run` is attempted.
* `docker ps -a` after the script exits (success or Ctrl-C) shows no leftover container from this run.
* Fully annotated where applicable (the file is a shell script; this criterion covers any Python touched, if any).

## [X] T004 `make run_in_docker`

### Description

Add a `run_in_docker` target to the `Makefile` that runs `bin/run_in_docker.sh`, following the existing `code`/`cloude_attach` pattern (thin target, all logic in the script). Update `.PHONY`.

### Acceptance criteria

* `make run_in_docker` from a clean checkout (no image built yet) builds and runs the container, dropping the user into the same interactive TUI `make run` would, using the same `.env`.
* Typing a message and getting a response works identically to `make run` (modulo the LLM backend being reachable from inside the container).

## [X] T005 Image housekeeping

### Description

Rebuilding must not let unused artifacts pile up over time:

* Confirm T003's fixed image tag means a rebuild replaces (retags) the previous image rather than leaving it dangling under `<none>:<none>` — this is Docker's default behavior when a tag is reused for a new build, but verify it explicitly rather than assuming.
* Document (in a short comment near the top of `bin/run_in_docker.sh` or in `README.md`, pick whichever fits better) the one command to reclaim space if a user wants to remove the image entirely (e.g. `docker rmi pico:local`), and the standard `docker image prune` for any dangling layers left by interrupted builds. Housekeeping here means "the normal workflow doesn't accumulate cruft," not building a custom cleanup tool.

### Acceptance criteria

* Building the image three times in a row (simulating repeated `make run_in_docker` use) leaves at most one `pico:local` image and zero dangling (`<none>`) images from those builds, per `docker images`.
* The cleanup command(s) are documented somewhere a user would actually find them (README or the script itself).

## [X] T006 `README.md`

### Description

Add a short section documenting `make run_in_docker`: what it does, that it needs `.env` set up the same as `make run` (point at `.env.example`), and that the session database persists across runs the same way. Keep it proportional to the existing README's depth — a few lines, not a new guide.

### Acceptance criteria

* A reader who has never used Docker with this project can get from `.env.example` to a running container using only the README.

## [X] T007 Verify and finalize

### Description

Run `make check` and fix everything until it is green — this milestone adds no Python, so this is mainly confirming nothing else broke. Then manually verify the full Docker path end-to-end: build clean, run, send a message, confirm a response, exit, confirm no leftover container, run again, confirm the prior conversation is still there.

### Acceptance criteria

* `make check` passes with no errors.
* The manual end-to-end walkthrough above succeeds with no undocumented manual steps beyond what `README.md` (T006) describes.

### Completion

Commit:
