#!/usr/bin/env sh
# Source this file from a Trilobite launcher to select sealed runtimes first.

TRILOBITE_RUNTIME_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "${TRILOBITE_HOME:-}" ]; then
  if [ -n "${XDG_DATA_HOME:-}" ]; then
    TRILOBITE_HOME="$XDG_DATA_HOME/trilobite"
  else
    TRILOBITE_HOME="${HOME:-$TRILOBITE_RUNTIME_ROOT}/.local/share/trilobite"
  fi
fi

case "$(uname -s 2>/dev/null || true)" in
  Darwin) trilobite_platform=macos ;;
  Linux) trilobite_platform=linux ;;
  *) trilobite_platform=unknown ;;
esac
case "$(uname -m 2>/dev/null || true)" in
  x86_64|amd64) trilobite_arch=x86_64 ;;
  arm64|aarch64) trilobite_arch=arm64 ;;
  *) trilobite_arch=unknown ;;
esac
trilobite_identity="$trilobite_platform-$trilobite_arch"
TRILOBITE_ENGINE_ROOT=${TRILOBITE_ENGINE_BUNDLE:-}
case "$TRILOBITE_ENGINE_ROOT" in
  */ENGINE-BUNDLE.json) TRILOBITE_ENGINE_ROOT=$(dirname -- "$TRILOBITE_ENGINE_ROOT") ;;
esac
if [ -z "$TRILOBITE_ENGINE_ROOT" ] && [ -f "$TRILOBITE_RUNTIME_ROOT/engine/$trilobite_identity/ENGINE-BUNDLE.json" ]; then
  TRILOBITE_ENGINE_ROOT="$TRILOBITE_RUNTIME_ROOT/engine/$trilobite_identity"
fi
if [ -z "$TRILOBITE_ENGINE_ROOT" ] && [ -f "$TRILOBITE_RUNTIME_ROOT/engine/ENGINE-BUNDLE.json" ]; then
  TRILOBITE_ENGINE_ROOT="$TRILOBITE_RUNTIME_ROOT/engine"
fi

TRILOBITE_PYTHON=
if [ -n "$TRILOBITE_ENGINE_ROOT" ]; then
  for candidate in \
    "$TRILOBITE_ENGINE_ROOT/runtime/python/bin/python3" \
    "$TRILOBITE_ENGINE_ROOT/runtime/python/python3" \
    "$TRILOBITE_ENGINE_ROOT/runtime/python/bin/python" \
    "$TRILOBITE_ENGINE_ROOT/runtime/python/python"; do
    if [ -x "$candidate" ]; then TRILOBITE_PYTHON=$candidate; break; fi
  done
fi
if [ -z "$TRILOBITE_PYTHON" ] && [ -x "$TRILOBITE_RUNTIME_ROOT/venv/bin/python3" ]; then
  TRILOBITE_PYTHON="$TRILOBITE_RUNTIME_ROOT/venv/bin/python3"
fi
if [ -z "$TRILOBITE_PYTHON" ]; then
  TRILOBITE_PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
fi

if [ -n "$TRILOBITE_ENGINE_ROOT" ] && [ -x "$TRILOBITE_ENGINE_ROOT/runtime/ollama/ollama" ]; then
  TRILOBITE_OLLAMA_EXE="$TRILOBITE_ENGINE_ROOT/runtime/ollama/ollama"
  PATH="$TRILOBITE_ENGINE_ROOT/runtime/ollama:$PATH"
  OLLAMA_MODELS=${OLLAMA_MODELS:-$TRILOBITE_HOME/ollama-models}
  OLLAMA_NO_CLOUD=1
else
  TRILOBITE_OLLAMA_EXE=${TRILOBITE_OLLAMA_EXE:-$(command -v ollama 2>/dev/null || true)}
fi

export TRILOBITE_RUNTIME_ROOT TRILOBITE_HOME TRILOBITE_ENGINE_ROOT
export TRILOBITE_PYTHON TRILOBITE_OLLAMA_EXE OLLAMA_MODELS OLLAMA_NO_CLOUD PATH
unset trilobite_platform trilobite_arch trilobite_identity candidate
