.PHONY: lint format typecheck test build check code claude_attach run run_in_docker evals stop_code

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run pyright

test:
	uv run pytest

build:
	uv build

check: lint typecheck test build

code:
	bin/code.sh

claude_attach:
	tmux attach -t claude-pico

run:
	uv run python -m pico

run_in_docker:
	bin/run_in_docker.sh

evals:
	uv run python -m pico.evals

stop_code:
	touch .stop_code
