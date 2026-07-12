#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$ROOT/trilobite-runtime.sh"
if [ -z "${TRILOBITE_PYTHON:-}" ]; then
  echo "[trilobite-launcher] ERROR: no Python runtime found." >&2
  exit 3
fi
: "${TRILOBITE_LAUNCHER_HOST:=127.0.0.1}"
: "${TRILOBITE_LAUNCHER_PORT:=11436}"
export TRILOBITE_LAUNCHER_HOST TRILOBITE_LAUNCHER_PORT
exec "$TRILOBITE_PYTHON" "$ROOT/trilobite_launcher.py" "$@"
