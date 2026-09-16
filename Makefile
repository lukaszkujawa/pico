.PHONY: lint format typecheck test build check code cloude_attach run run_in_docker stop_code

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

cloude_attach:
	tmux attach -t claude-pico

run:
	uv run python -m pico

run_in_docker:
	bin/run_in_docker.sh

stop_code:
	touch .stop_code
