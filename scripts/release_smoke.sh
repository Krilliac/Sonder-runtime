#!/usr/bin/env sh
# Pre-tag release smoke check: version-policy gate plus a real end-to-end
# smoke run, chained into one pass/fail result
# (docs/runbooks/release-smoke-check.md). POSIX counterpart of
# scripts/release_smoke.ps1; keep both in sync.
#
#   scripts/release_smoke.sh
#   scripts/release_smoke.sh --tag app-v1.2.3 \
#       --revision 0123456789abcdef0123456789abcdef01234567 --require-release
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

TAG=""
REVISION=""
REQUIRE_RELEASE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --revision) REVISION="$2"; shift 2 ;;
    --require-release) REQUIRE_RELEASE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PY="${SONDER_PYTHON:-}"
if [ -z "$PY" ] && [ -x "$REPO/venv/bin/python" ]; then
  PY="$REPO/venv/bin/python"
fi
if [ -z "$PY" ]; then
  PY=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
fi
if [ -z "$PY" ]; then
  echo "no Python interpreter found; set SONDER_PYTHON or install python3" >&2
  exit 3
fi

FAILED=""

echo "[release-smoke] checking runtime/app/tag version policy..."
set -- "$REPO/scripts/check_release_version.py" --json
[ -n "$TAG" ] && set -- "$@" --tag "$TAG"
[ -n "$REVISION" ] && set -- "$@" --revision "$REVISION"
[ "$REQUIRE_RELEASE" -eq 1 ] && set -- "$@" --require-release
if ! "$PY" "$@"; then
  FAILED="${FAILED}version policy; "
fi

echo
echo "[release-smoke] running end-to-end smoke (config, migrations, store round-trip)..."
if ! "$PY" -m sonder_runtime smoke --skip-ollama; then
  FAILED="${FAILED}runtime smoke; "
fi

echo
if [ -n "$FAILED" ]; then
  echo "[release-smoke] FAIL: $FAILED"
  exit 1
fi
echo "[release-smoke] PASS: version policy and runtime smoke both succeeded"
exit 0
