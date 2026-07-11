#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/trilobite-runtime.sh"
if [ -z "${TRILOBITE_PYTHON:-}" ]; then
  echo "[trilobite] ERROR: no bundled or system Python runtime was found." >&2
  exit 3
fi
exec "$TRILOBITE_PYTHON" "$SCRIPT_DIR/trilobite_headless.py" "$@"
