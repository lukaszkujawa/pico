.PHONY: lint format typecheck test check code run stop_code

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

check: lint typecheck test

code:
	bin/code.sh

run:
	uv run python -m pico

stop_code:
	touch .stop_code
