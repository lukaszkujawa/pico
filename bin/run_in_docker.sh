#!/usr/bin/env bash

# Usage: run_in_docker.sh [--debug] [--ctx TOKENS] [--prompt "task"]
#   --debug   run pico with --debug and mount ./logs so run logs land outside the container
#   --ctx     override LLM_CONTEXT_SIZE from .env
#   --prompt  submit the given prompt automatically once the TUI opens
#
# Reclaim space: `docker rmi pico:local` removes the image;
# `docker image prune` clears any dangling layers from interrupted builds.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="pico:local"
ENV_FILE="$ROOT_DIR/.env"
DATA_DIR="$ROOT_DIR/.docker_data"
LOGS_DIR="$ROOT_DIR/logs"

DOCKER_ARGS=()
APP_ARGS=()

usage_error() {
  echo "pico: $1" >&2
  echo "usage: run_in_docker.sh [--debug] [--ctx TOKENS] [--prompt \"task\"]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --debug)
      APP_ARGS+=(--debug)
      DOCKER_ARGS+=(-v "$LOGS_DIR:/app/logs")
      mkdir -p "$LOGS_DIR"
      ;;
    --ctx)
      [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || usage_error "--ctx requires a token count"
      DOCKER_ARGS+=(-e "LLM_CONTEXT_SIZE=$2")
      shift
      ;;
    --prompt)
      [[ $# -ge 2 && -n "$2" ]] || usage_error "--prompt requires a non-empty prompt"
      APP_ARGS+=(--prompt "$2")
      shift
      ;;
    *)
      usage_error "unknown option: $1"
      ;;
  esac
  shift
done

cd "$ROOT_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "pico: missing required file: $ENV_FILE" >&2
  exit 1
fi

mkdir -p "$DATA_DIR"

docker build -t "$IMAGE" . || exit $?

docker run --rm -it \
  --env-file "$ENV_FILE" \
  -e SESSION_DB_PATH=/data/pico.db \
  -v "$DATA_DIR:/data" \
  ${DOCKER_ARGS[@]+"${DOCKER_ARGS[@]}"} \
  "$IMAGE" \
  ${APP_ARGS[@]+"${APP_ARGS[@]}"}
