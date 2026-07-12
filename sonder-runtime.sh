#!/usr/bin/env sh
# Source this file from a Sonder launcher to select sealed runtimes first.

SONDER_RUNTIME_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -z "${SONDER_HOME:-}" ]; then
  if [ -n "${XDG_DATA_HOME:-}" ]; then
    SONDER_HOME="$XDG_DATA_HOME/sonder"
  else
    SONDER_HOME="${HOME:-$SONDER_RUNTIME_ROOT}/.local/share/sonder"
  fi
fi

case "$(uname -s 2>/dev/null || true)" in
  Darwin) sonder_platform=macos ;;
  Linux) sonder_platform=linux ;;
  *) sonder_platform=unknown ;;
esac
case "$(uname -m 2>/dev/null || true)" in
  x86_64|amd64) sonder_arch=x86_64 ;;
  arm64|aarch64) sonder_arch=arm64 ;;
  *) sonder_arch=unknown ;;
esac
sonder_identity="$sonder_platform-$sonder_arch"
SONDER_ENGINE_ROOT=${SONDER_ENGINE_BUNDLE:-}
case "$SONDER_ENGINE_ROOT" in
  */ENGINE-BUNDLE.json) SONDER_ENGINE_ROOT=$(dirname -- "$SONDER_ENGINE_ROOT") ;;
esac
if [ -z "$SONDER_ENGINE_ROOT" ] && [ -f "$SONDER_RUNTIME_ROOT/engine/$sonder_identity/ENGINE-BUNDLE.json" ]; then
  SONDER_ENGINE_ROOT="$SONDER_RUNTIME_ROOT/engine/$sonder_identity"
fi
if [ -z "$SONDER_ENGINE_ROOT" ] && [ -f "$SONDER_RUNTIME_ROOT/engine/ENGINE-BUNDLE.json" ]; then
  SONDER_ENGINE_ROOT="$SONDER_RUNTIME_ROOT/engine"
fi

SONDER_PYTHON=
if [ -n "$SONDER_ENGINE_ROOT" ]; then
  for candidate in \
    "$SONDER_ENGINE_ROOT/runtime/python/bin/python3" \
    "$SONDER_ENGINE_ROOT/runtime/python/python3" \
    "$SONDER_ENGINE_ROOT/runtime/python/bin/python" \
    "$SONDER_ENGINE_ROOT/runtime/python/python"; do
    if [ -x "$candidate" ]; then SONDER_PYTHON=$candidate; break; fi
  done
fi
if [ -z "$SONDER_PYTHON" ] && [ -x "$SONDER_RUNTIME_ROOT/venv/bin/python3" ]; then
  SONDER_PYTHON="$SONDER_RUNTIME_ROOT/venv/bin/python3"
fi
if [ -z "$SONDER_PYTHON" ]; then
  SONDER_PYTHON=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
fi

if [ -n "$SONDER_ENGINE_ROOT" ] && [ -x "$SONDER_ENGINE_ROOT/runtime/ollama/ollama" ]; then
  SONDER_OLLAMA_EXE="$SONDER_ENGINE_ROOT/runtime/ollama/ollama"
  PATH="$SONDER_ENGINE_ROOT/runtime/ollama:$PATH"
  OLLAMA_MODELS=${OLLAMA_MODELS:-$SONDER_HOME/ollama-models}
  OLLAMA_NO_CLOUD=1
else
  SONDER_OLLAMA_EXE=${SONDER_OLLAMA_EXE:-$(command -v ollama 2>/dev/null || true)}
fi

export SONDER_RUNTIME_ROOT SONDER_HOME SONDER_ENGINE_ROOT
export SONDER_PYTHON SONDER_OLLAMA_EXE OLLAMA_MODELS OLLAMA_NO_CLOUD PATH
unset sonder_platform sonder_arch sonder_identity candidate
