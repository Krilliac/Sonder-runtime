#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/trilobite-runtime.sh"
if [ -z "${TRILOBITE_PYTHON:-}" ]; then
  echo "[trilobite] ERROR: no bundled or system Python runtime was found." >&2
  exit 3
fi
if ! "${TRILOBITE_OLLAMA_EXE:-ollama}" show trilobite >/dev/null 2>&1; then
  "$TRILOBITE_PYTHON" "$SCRIPT_DIR/bootstrap_engine.py"
fi
exec "$TRILOBITE_PYTHON" "$SCRIPT_DIR/trilobite_serve.py" "$@"
