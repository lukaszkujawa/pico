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

To reclaim space, remove the image with `docker rmi pico:local`, or clear any dangling build layers with `docker image prune`.
