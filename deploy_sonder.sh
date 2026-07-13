#!/usr/bin/env bash
# deploy_sonder.sh — provision Sonder Runtime's local Ollama inference alias on
# this Ubuntu box and optionally host the OpenAI-compatible runtime service.
#
# Inference alias only (default):  bash deploy_sonder.sh
# Alias + runtime service:         bash deploy_sonder.sh --serve
# Runtime service only (repo already cloned + alias already provisioned):
#                             bash deploy_sonder.sh --serve-only
#
# Run ON THE SERVER (as root). --serve/--serve-only expect this script to be
# sitting inside a checkout of the Sonder Runtime repo (they set up a venv and a
# systemd unit next to it) — clone the repo first if you haven't:
#   git clone https://github.com/Krilliac/Sonder-runtime.git && cd Sonder-runtime
#
# Env vars for the hosting section:
#   SONDER_API_KEY   API key clients must send (auto-generated if unset)
#   SONDER_PORT      port to bind (default 11435)
# Performance knobs used by local Ollama requests:
#   SONDER_NUM_THREAD CPU threads per local model request (default: nproc)
#   SONDER_NUM_GPU    GPU layers to offload (default: 999/all, use 0 for CPU)
#   SONDER_NUM_BATCH  inference batch size (default: 512)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required for the fail-closed Ollama endpoint preflight" >&2
  exit 1
fi
CLIENT_OLLAMA_HOST="$(PYTHONPATH="$SCRIPT_DIR" python3 -c 'import ollama_endpoint; print(ollama_endpoint.configured_origin(allow_remote=False))')"

export SONDER_NUM_THREAD="${SONDER_NUM_THREAD:-$(nproc 2>/dev/null || echo 4)}"
export SONDER_NUM_GPU="${SONDER_NUM_GPU:-999}"
export SONDER_NUM_BATCH="${SONDER_NUM_BATCH:-512}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"

SERVE=0
MODEL_STEP=1
for arg in "$@"; do
  case "$arg" in
    --serve)      SERVE=1 ;;
    --serve-only) SERVE=1; MODEL_STEP=0 ;;
    *) echo "unknown flag: $arg (expected --serve or --serve-only)" >&2; exit 1 ;;
  esac
done

if [ "$MODEL_STEP" -eq 1 ]; then

echo "== 1/4 Ollama =="
if ! command -v ollama >/dev/null 2>&1; then
  # Download the official installer to a file first so it CAN be inspected,
  # rather than piping a remote script straight into a root shell.
  INSTALLER="$(mktemp)"
  curl -fsSL https://ollama.com/install.sh -o "$INSTALLER"
  echo "Ollama installer downloaded to $INSTALLER (review it if you like), running it..."
  sh "$INSTALLER"
  rm -f "$INSTALLER"
fi
# make sure the server is up
(systemctl start ollama 2>/dev/null || (nohup ollama serve >/var/log/ollama.log 2>&1 &)) || true
sleep 3

echo "== 2/4 pick model by RAM =="
RAM_GB=$(free -g | awk '/Mem:/{print $2}')
if   [ "${RAM_GB:-0}" -ge 8 ]; then BASE="qwen2.5-coder:7b"
elif [ "${RAM_GB:-0}" -ge 4 ]; then BASE="qwen2.5-coder:3b"
else                                BASE="qwen2.5-coder:1.5b"
fi
echo "detected ${RAM_GB}GB RAM -> base model: $BASE"

echo "== 3/4 pull models (this downloads a few GB) =="
OLLAMA_HOST="$CLIENT_OLLAMA_HOST" ollama pull "$BASE"
OLLAMA_HOST="$CLIENT_OLLAMA_HOST" ollama pull nomic-embed-text

echo "== 4/4 create the stable sonder:latest Ollama alias =="
MF="$(mktemp)"
cat > "$MF" <<EOF
FROM $BASE
PARAMETER temperature 0.2
SYSTEM """You are the local language model operating inside Sonder Runtime. Sonder Runtime is the host orchestration software, not a foundation model or a set of weights. When the runtime invokes you, it may supply grounded lessons, memory, guarded tools, and policy; use only capabilities explicitly exposed for the current request. When invoked directly through Ollama, you provide local inference without those runtime capabilities.

Be direct, honest, and concrete. Never fabricate capabilities, tools, results, or configuration. Do not expose hidden chain-of-thought; report observable actions and evidence. Prefer correct, working code and keep answers concise."""
EOF
OLLAMA_HOST="$CLIENT_OLLAMA_HOST" ollama create sonder -f "$MF"
rm -f "$MF"

echo ""
echo "DONE. The sonder:latest Ollama rollback alias is ready on this box."
echo "  Direct alias (bypasses runtime memory/tools): ollama run sonder"
echo "  Direct Ollama API (also bypasses the runtime): curl http://127.0.0.1:11434/api/chat -d '{\"model\":\"sonder\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"stream\":false}'"
echo ""
if [ "$SERVE" -eq 0 ]; then
  echo "NEXT (Sonder Runtime): copy the repository here and run its"
  echo "server/REPL/proxy — the runtime adds retrieval, capture, /train, trace, and the"
  echo "OpenAI-compatible proxy. Re-run this script with --serve to host it as a"
  echo "public systemd service, or ask Claude to help set up the code transfer."
fi

fi  # MODEL_STEP

if [ "$SERVE" -eq 1 ]; then

echo ""
echo "== hosting: Sonder Runtime as a public systemd service =="

# This script must live inside the cloned repo (it references sibling files
# like sonder_serve.py). Resolve that directory so the service works
# regardless of cwd.
CLONE_DIR="$SCRIPT_DIR"
if [ ! -f "$CLONE_DIR/sonder_serve.py" ]; then
  echo "ERROR: $CLONE_DIR/sonder_serve.py not found." >&2
  echo "  --serve expects this script to be run from inside a checkout of" >&2
  echo "  https://github.com/Krilliac/Sonder-runtime — clone it first:" >&2
  echo "    git clone https://github.com/Krilliac/Sonder-runtime.git && cd Sonder-runtime && bash deploy_sonder.sh --serve" >&2
  exit 1
fi

echo "-- installing Python3 + venv + pip --"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip
else
  echo "no supported package manager found (apt/dnf/yum) — install python3/venv/pip manually" >&2
  exit 1
fi

echo "-- creating venv in $CLONE_DIR/venv --"
if [ ! -d "$CLONE_DIR/venv" ]; then
  python3 -m venv "$CLONE_DIR/venv"
fi
VENV_PY="$CLONE_DIR/venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install mcp

echo "-- resolving API key --"
KEY="${SONDER_API_KEY:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)}"
PORT="${SONDER_PORT:-11435}"

echo "-- writing systemd unit /etc/systemd/system/sonder.service --"
cat > /etc/systemd/system/sonder.service <<EOF
[Unit]
Description=Sonder Runtime OpenAI-compatible proxy
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CLONE_DIR
Environment=SONDER_HOST=0.0.0.0
Environment=SONDER_API_KEY=$KEY
Environment=SONDER_NUM_THREAD=$SONDER_NUM_THREAD
Environment=SONDER_NUM_GPU=$SONDER_NUM_GPU
Environment=SONDER_NUM_BATCH=$SONDER_NUM_BATCH
Environment=OLLAMA_FLASH_ATTENTION=$OLLAMA_FLASH_ATTENTION
ExecStart=$VENV_PY $CLONE_DIR/sonder_serve.py $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now sonder

SERVER_IP="$(curl -fsSL -4 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "<server-ip>")"

echo ""
echo "DONE. Sonder Runtime is hosted as a public systemd service."
echo "  Public URL:  http://${SERVER_IP}:${PORT}/v1"
echo "  API key:     ${KEY}"
echo ""
echo "  Give clients the URL + key above (see CLIENT.md)."
echo "  REMINDER: open the firewall / cloud security-group for port ${PORT},"
echo "  and keep that API key secret — it is the ONLY thing protecting this"
echo "  server from anyone on the internet who finds the port."
echo ""
echo "  Manage:  systemctl status sonder | journalctl -u sonder -f"

fi  # SERVE
