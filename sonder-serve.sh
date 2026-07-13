#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/sonder-runtime.sh"
if [ -z "${SONDER_PYTHON:-}" ]; then
  echo "[sonder] ERROR: no bundled or system Python runtime was found." >&2
  exit 3
fi
"$SONDER_PYTHON" "$SCRIPT_DIR/sonder_headless.py" engine
exec "$SONDER_PYTHON" "$SCRIPT_DIR/sonder_serve.py" "$@"
