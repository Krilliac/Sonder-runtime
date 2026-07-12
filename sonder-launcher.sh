#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$ROOT/sonder-runtime.sh"
if [ -z "${SONDER_PYTHON:-}" ]; then
  echo "[sonder-launcher] ERROR: no Python runtime found." >&2
  exit 3
fi
: "${SONDER_LAUNCHER_HOST:=127.0.0.1}"
: "${SONDER_LAUNCHER_PORT:=11436}"
export SONDER_LAUNCHER_HOST SONDER_LAUNCHER_PORT
exec "$SONDER_PYTHON" "$ROOT/sonder_launcher.py" "$@"
