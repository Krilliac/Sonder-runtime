#!/usr/bin/env bash
# deploy_trilobite.sh — stand up the trilobite MODEL on this Ubuntu box, and
# optionally host the full OpenAI-compatible proxy as a public systemd service.
#
# Model only (default):      bash deploy_trilobite.sh
# Model + hosted service:    bash deploy_trilobite.sh --serve
# Hosted service ONLY (repo already cloned + model already deployed):
#                             bash deploy_trilobite.sh --serve-only
#
# Run ON THE SERVER (as root). --serve/--serve-only expect this script to be
# sitting inside a checkout of the trilobite repo (they set up a venv and a
# systemd unit next to it) — clone the repo first if you haven't:
#   git clone https://github.com/Krilliac/Trilobite-model.git && cd Trilobite-model
#
# Env vars for the hosting section:
#   TRILOBITE_API_KEY   API key clients must send (auto-generated if unset)
#   TRILOBITE_PORT      port to bind (default 11435)
# Performance knobs used by local Ollama requests:
#   LOCAL_LLM_NUM_THREAD CPU threads per local model request (default: nproc)
#   LOCAL_LLM_NUM_GPU    GPU layers to offload (default: 999/all, use 0 for CPU)
#   LOCAL_LLM_NUM_BATCH  inference batch size (default: 512)
set -euo pipefail

export LOCAL_LLM_NUM_THREAD="${LOCAL_LLM_NUM_THREAD:-$(nproc 2>/dev/null || echo 4)}"
export LOCAL_LLM_NUM_GPU="${LOCAL_LLM_NUM_GPU:-999}"
export LOCAL_LLM_NUM_BATCH="${LOCAL_LLM_NUM_BATCH:-512}"
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
ollama pull "$BASE"
ollama pull nomic-embed-text

echo "== 4/4 create the self-aware trilobite alias =="
MF="$(mktemp)"
cat > "$MF" <<EOF
FROM $BASE
PARAMETER temperature 0.2
SYSTEM """You are trilobite, a self-improving coding assistant that runs entirely locally on this machine through Ollama. There is no external server and no cloud, all inference happens here, privately. You are built on a Qwen2.5-Coder base, wrapped by a local system that gives you a growing memory of short 'lessons' distilled from past coding work; relevant lessons are retrieved and added to new tasks, and solutions that pass real tests are recorded so their lessons get reused. That is how you improve over time.

Be direct, honest, and concrete. Never fabricate capabilities, tools, or configuration you do not have: you have no web search, no web fetch, and no toggleable feature flags, do not invent JSON like that. When asked about yourself, describe what you actually are (above). You cannot read your own neural internals, so do not claim to, but do not fall back on canned 'as an AI language model I cannot' refusals either; just answer plainly and usefully. Prefer correct, working code and keep answers concise."""
EOF
ollama create trilobite -f "$MF"
rm -f "$MF"

echo ""
echo "DONE. trilobite is live on this box."
echo "  Chat:        ollama run trilobite"
echo "  HTTP API:    curl http://127.0.0.1:11434/api/chat -d '{\"model\":\"trilobite\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}],\"stream\":false}'"
echo ""
if [ "$SERVE" -eq 0 ]; then
  echo "NEXT (full self-improving system): copy the trilobite repo here and run its"
  echo "server/REPL/proxy — that adds retrieval, capture, /train, trace, and the"
  echo "OpenAI-compatible proxy. Re-run this script with --serve to host it as a"
  echo "public systemd service, or ask Claude to help set up the code transfer."
fi

fi  # MODEL_STEP

if [ "$SERVE" -eq 1 ]; then

echo ""
echo "== hosting: trilobite as a public systemd service =="

# This script must live inside the cloned repo (it references sibling files
# like trilobite_serve.py). Resolve that directory so the service works
# regardless of cwd.
CLONE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "$CLONE_DIR/trilobite_serve.py" ]; then
  echo "ERROR: $CLONE_DIR/trilobite_serve.py not found." >&2
  echo "  --serve expects this script to be run from inside a checkout of" >&2
  echo "  https://github.com/Krilliac/Trilobite-model — clone it first:" >&2
  echo "    git clone https://github.com/Krilliac/Trilobite-model.git && cd Trilobite-model && bash deploy_trilobite.sh --serve" >&2
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
KEY="${TRILOBITE_API_KEY:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)}"
PORT="${TRILOBITE_PORT:-11435}"

echo "-- writing systemd unit /etc/systemd/system/trilobite.service --"
cat > /etc/systemd/system/trilobite.service <<EOF
[Unit]
Description=trilobite OpenAI-compatible proxy
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CLONE_DIR
Environment=TRILOBITE_HOST=0.0.0.0
Environment=TRILOBITE_API_KEY=$KEY
Environment=LOCAL_LLM_NUM_THREAD=$LOCAL_LLM_NUM_THREAD
Environment=LOCAL_LLM_NUM_GPU=$LOCAL_LLM_NUM_GPU
Environment=LOCAL_LLM_NUM_BATCH=$LOCAL_LLM_NUM_BATCH
Environment=OLLAMA_FLASH_ATTENTION=$OLLAMA_FLASH_ATTENTION
ExecStart=$VENV_PY $CLONE_DIR/trilobite_serve.py $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now trilobite

SERVER_IP="$(curl -fsSL -4 ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "<server-ip>")"

echo ""
echo "DONE. trilobite is hosted as a public systemd service."
echo "  Public URL:  http://${SERVER_IP}:${PORT}/v1"
echo "  API key:     ${KEY}"
echo ""
echo "  Give clients the URL + key above (see CLIENT.md)."
echo "  REMINDER: open the firewall / cloud security-group for port ${PORT},"
echo "  and keep that API key secret — it is the ONLY thing protecting this"
echo "  server from anyone on the internet who finds the port."
echo ""
echo "  Manage:  systemctl status trilobite | journalctl -u trilobite -f"

fi  # SERVE
