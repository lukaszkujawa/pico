# Pico

Pico is an agentic harness for local and smaller language models. Its goal is to let modest models complete complex, long-running tasks with reliability approaching frontier models — through a strong runtime rather than model scale. The model reasons and decides; the runtime remembers, organises, computes, verifies, and manages context. See `VISION.md` for the principles.

## How it works

Context is treated as a cache, not memory. Every model call receives a compiled view of current state — a recency window of the conversation plus a briefing with the current plan and an index of facts — sized to the model's context budget. Bulky content is elided or demoted to handles that the model can recover on demand.

The agent works with a small vocabulary of tools:

- **Files** — `read_file` (sliced and capped), `edit_file` (exact-text replacement), `write_file`.
- **Shell** — run commands, with results recorded as facts.
- **Facts** — `note` records durable findings, optionally verified by a shell check that must pass before the note becomes a fact; `search_facts` and `read_fact` recover them after they fall out of context.
- **Plans** — `set_plan` and `complete_step`; the runtime runs each step in a fresh agent.
- **Scratch SQL** — `load_table` and `sql` push filtering and aggregation into SQLite instead of the context window.
- **Delegation** — `delegate` decomposes work into isolated subagents with small contexts and typed results.
- **Vision** — `view_image` looks at a png, jpeg, gif, or webp when `LLM_VISION=1` and the model is multimodal; only the latest image stays in the prompt.
- **Answer** — finish the run with a final response.

Everything is recorded as events in a SQLite session database, so state survives the context window and sessions can be resumed.

## Usage

Copy `.env.example` to `.env` and fill in your model server details:

```
cp .env.example .env
```

`LLM_VENDOR` selects the client: `ollama`, `openai` (any OpenAI-compatible server, e.g. llama.cpp or vLLM), or `anthropic`. `LLM_CONTEXT_SIZE` drives the context budget; `LLM_TEMPERATURE` is optional, and `LLM_VISION=1` enables `view_image` for multimodal models.

Then run the TUI:

```
uv run python -m pico
```

Flags: `--debug` writes per-run logs under `logs/`, `--prompt` submits an initial prompt, `--resume` continues a session (see below), `--sock NAME` accepts prompts written to a FIFO. Inside the TUI, `/model` switches the model for the next run, `/quit` exits, and `ctrl+n` starts a fresh conversation.

## Sessions

Each launch starts a fresh conversation with a new session id, shown under the logo. Earlier conversations stay in the session database at `SESSION_DB_PATH`.

```
uv run python -m pico --resume            # the most recent conversation
uv run python -m pico --resume <session>  # a specific session id
```

## Running in Docker

`make run_in_docker` runs Pico in a container with no local Python or `uv` needed. It builds the image (tagged `pico:local`) and drops you into the same TUI, reading the same `.env`. The session database persists in `.docker_data/` on the host, so previous runs stay resumable by id.

Pass extra flags through `ARGS`. `--sock NAME` lets you drive the running container from another terminal — prompts written to `./sock/NAME` on the host are forwarded into the container and submitted as if typed:

```
make run_in_docker ARGS="--debug --sock 0"
echo "What is 17 * 23?" > ./sock/0
```

To reclaim space, `docker rmi pico:local` or `docker image prune`.

## Evals

The eval suite measures whether the runtime helps a small model, not whether the model is clever: a fixed set of tasks (read, search, write, run a script, aggregate a file too large for the context budget, recall evidence from a long log, iterate until a seeded test passes, a multi-step chore), each with a deterministic pass/fail check. Nothing is judged by a model.

It needs a live model, so it is not part of `make check`. With a working `.env`:

```
make evals
```

Each task runs one headless turn in a fresh temporary directory with a throwaway session database. Results print as a table and land in `eval_results/<timestamp>.json`. Evals report, they do not gate.

## Development

Milestones live in `tasks/todo/`, one per file, and move to `tasks/done/` when complete. `make check` runs lint, strict type checking, dead-code and architecture checks, tests, and a build — it must stay green. `make format` auto-fixes formatting and lint.

`make code` runs milestones unattended: each pending milestone is claimed into its own git worktree, given to a Claude Code agent driven by `tasks/PROMPT.md`, and merged into master when the agent completes it. `make code_attach` watches a running agent live; `make stop_code` stops the loop after the current step. Logs land in `logs-code/`.
