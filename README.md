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

## Running in Docker

`make run_in_docker` runs Pico in a container, with no local Python or `uv` install needed. It builds the image (tagged `pico:local`, reusing that tag on rebuild) and drops you into the same interactive TUI as `make run`, reading the same `.env` — see above to set one up first. The session database persists across runs the same way, in `.docker_data/` on the host.

To reclaim space, remove the image with `docker rmi pico:local`, or clear any dangling build layers with `docker image prune`.
