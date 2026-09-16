#!/usr/bin/env bash

# Reclaim space: `docker rmi pico:local` removes the image;
# `docker image prune` clears any dangling layers from interrupted builds.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="pico:local"
ENV_FILE="$ROOT_DIR/.env"
DATA_DIR="$ROOT_DIR/.docker_data"

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
  "$IMAGE"
