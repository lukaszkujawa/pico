.PHONY: lint format typecheck deadcode arch test build check code code_attach run run_in_docker evals stop_code wipe_logs

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run pyright

deadcode:
	uv run vulture

arch:
	uv run tach check

test:
	uv run pytest

build:
	uv build

check: lint typecheck deadcode arch test build

code:
	bin/code.sh

code_attach:
	@if [ -n "$(TASK)" ]; then \
		tmux attach -t "claude-pico-$(TASK)"; \
	else \
		sessions="$$(tmux list-sessions -F '#S' 2>/dev/null | grep '^claude-pico-')"; \
		count="$$(printf '%s\n' "$$sessions" | grep -c . )"; \
		if [ "$$count" -eq 0 ]; then echo "no agent sessions running"; exit 1; \
		elif [ "$$count" -eq 1 ]; then tmux attach -t "$$sessions"; \
		else printf '%s\n' "$$sessions"; echo "attach with: make code_attach TASK=<name>"; fi \
	fi

run:
	uv run python -m pico

# Example
# make run_in_docker ARGS="--debug --ctx 16000 --prompt 'Fetch code from https://github.com/lukaszkujawa/pico and review it. Install tools if you need any, you are root.'"
#
# --sock NAME accepts prompts on ./sock/NAME while the container runs:
# make run_in_docker ARGS="--debug --sock 0"
# echo "What is 17 * 23?" > ./sock/0
run_in_docker:
	bin/run_in_docker.sh $(ARGS)

evals:
	uv run python -m pico.evals

stop_code:
	touch .stop_code

wipe_logs:
	rm -rf logs logs-code
