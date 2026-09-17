# Pico

## Usage

Copy `.env.example` to `.env` and fill in your Ollama server details:

```
cp .env.example .env
```

Then run:

```
uv run python -m pico
```

## Sessions

Each launch starts a fresh, empty conversation with a new session id, shown under the logo at startup. Earlier conversations are never deleted — they stay in the session database at `SESSION_DB_PATH`.

To continue where you left off:

```
uv run python -m pico --resume            # the most recent conversation
uv run python -m pico --resume <session>  # a specific session id
```

Press `ctrl+n` at any time (except mid-turn) to start a new conversation without quitting. The transcript clears and the session id under the logo updates; the previous conversation stays resumable by its id.

## Running in Docker

`make run_in_docker` runs Pico in a container, with no local Python or `uv` install needed. It builds the image (tagged `pico:local`, reusing that tag on rebuild) and drops you into the same interactive TUI as `make run`, reading the same `.env` — see above to set one up first. The session database persists across runs the same way, in `.docker_data/` on the host — so two consecutive runs are independent conversations that you can still `--resume` by id (see above).

Pass extra flags through `ARGS`. `--sock NAME` lets you drive the running container from another terminal: prompts written to `./sock/NAME` on the host are submitted as if you had typed them.

```
make run_in_docker ARGS="--debug --sock 0"
echo "What is 17 * 23?" > ./sock/0
```

A FIFO on a bind mount is not shared across the container boundary, so the FIFO Pico reads lives on a tmpfs inside the container and the host-side `./sock/NAME` forwards each line into it. The forwarder holds prompts written before the TUI is ready and delivers them once it is, then removes `./sock/NAME` when the container exits.

To reclaim space, remove the image with `docker rmi pico:local`, or clear any dangling build layers with `docker image prune`.

## Evals

The eval suite measures whether the runtime helps a small model, not whether the model is clever. It is a fixed set of tasks — read a file, search several files, write a file, run a script, aggregate a file too large for the context budget, recall evidence buried in a long log, iterate until a seeded test script passes, and a multi-step chore — each with a pass/fail check written in ordinary Python. Nothing is judged by a model.

The suite needs a live model, so it is not part of `make check`. With a working `.env` (see Usage above), run:

```
make evals
```

Each task gets a fresh temporary directory, a throwaway session database, and one headless turn against the configured model. The run prints a table of task, pass/fail, iterations, tool calls, prompt and completion tokens, and seconds, and writes the same data plus the model and context size to `eval_results/<timestamp>.json`. That directory is gitignored. The exit code is always 0 — evals report, they do not gate.

Milestones that change runtime behaviour — budgets, thresholds, context compilation, delegation — should quote suite results from before and after the change in their completion notes whenever a live model is available.
