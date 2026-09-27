#!/usr/bin/env sh
# Fast regression loop: tests selected from the change, under xdist worksteal.
#
#   scripts/test-fast.sh                  change since merge-base with origin/main
#   scripts/test-fast.sh --working-tree   uncommitted change only
#   scripts/test-fast.sh --all            full suite
#   scripts/test-fast.sh -- -k pattern    anything after -- goes to pytest
#
# Resolves the interpreter the way scripts/run-tests.cmd does: SONDER_PYTHON,
# else the checkout's venv, else python3 on PATH. See scripts/test_fast.py.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(dirname -- "$SCRIPT_DIR")
PY=${SONDER_PYTHON:-}
if [ -z "$PY" ]; then
  if [ -x "$REPO/venv/bin/python" ]; then
    PY="$REPO/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    echo "ERROR: no Python interpreter found; set SONDER_PYTHON or create $REPO/venv" >&2
    exit 3
  fi
fi
exec "$PY" "$SCRIPT_DIR/test_fast.py" "$@"
