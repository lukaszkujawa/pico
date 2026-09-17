#!/usr/bin/env bash

# Usage: run_in_docker.sh [--debug] [--ctx TOKENS] [--prompt "task"] [--sock NAME]
#   --debug   run pico with --debug and mount ./logs so run logs land outside the container
#   --ctx     override LLM_CONTEXT_SIZE from .env
#   --prompt  submit the given prompt automatically once the TUI opens
#   --sock    accept prompts on ./sock/NAME, e.g. `echo "task" > ./sock/0`
#
# Reclaim space: `docker rmi pico:local` removes the image;
# `docker image prune` clears any dangling layers from interrupted builds.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="pico:local"
ENV_FILE="$ROOT_DIR/.env"
DATA_DIR="$ROOT_DIR/.docker_data"
LOGS_DIR="$ROOT_DIR/logs"
SOCK_DIR="$ROOT_DIR/sock"
CONTAINER_SOCK_DIR="/run/pico"

DOCKER_ARGS=()
APP_ARGS=()
SOCK_NAME=""
CONTAINER_NAME=""
FORWARDER_PID=""

usage_error() {
  echo "pico: $1" >&2
  echo "usage: run_in_docker.sh [--debug] [--ctx TOKENS] [--prompt \"task\"] [--sock NAME]" >&2
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
    --sock)
      [[ $# -ge 2 && "$2" =~ ^[A-Za-z0-9._-]+$ ]] || usage_error "--sock requires a file name"
      SOCK_NAME="$2"
      APP_ARGS+=(--sock "$CONTAINER_SOCK_DIR/$SOCK_NAME")
      CONTAINER_NAME="pico-$$"
      DOCKER_ARGS+=(--name "$CONTAINER_NAME" --tmpfs "$CONTAINER_SOCK_DIR")
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

# A FIFO on a bind mount is not shared across the container boundary: the host and
# the container end up with separate pipes at the same path and writes never arrive.
# So pico's FIFO stays on a tmpfs inside the container and a host-side FIFO forwards
# each line into it with `docker exec`.
forward_sock() {
  local host_fifo="$SOCK_DIR/$SOCK_NAME"
  local target="$CONTAINER_SOCK_DIR/$SOCK_NAME"
  rm -f "$host_fifo"
  mkdir -p "$SOCK_DIR"
  mkfifo "$host_fifo" || return

  exec 3<>"$host_fifo"
  while read -r line <&3; do
    [[ -n "$line" ]] || continue
    until docker exec "$CONTAINER_NAME" test -p "$target" 2>/dev/null; do
      docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null \
        | grep -q true || return
      sleep 0.2
    done
    docker exec -i "$CONTAINER_NAME" sh -c "cat > '$target'" <<<"$line" 2>/dev/null
  done
}

cleanup() {
  [[ -n "$FORWARDER_PID" ]] && kill "$FORWARDER_PID" 2>/dev/null
  [[ -n "$SOCK_NAME" ]] && rm -f "$SOCK_DIR/$SOCK_NAME"
  return 0
}
trap cleanup EXIT

docker build -t "$IMAGE" . || exit $?

if [[ -n "$SOCK_NAME" ]]; then
  forward_sock &
  FORWARDER_PID=$!
fi

docker run --rm -it \
  --env-file "$ENV_FILE" \
  -e SESSION_DB_PATH=/data/pico.db \
  -v "$DATA_DIR:/data" \
  ${DOCKER_ARGS[@]+"${DOCKER_ARGS[@]}"} \
  "$IMAGE" \
  ${APP_ARGS[@]+"${APP_ARGS[@]}"}
