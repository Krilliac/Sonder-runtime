#!/usr/bin/env bash
# cloud_train.sh — one-shot QLoRA adapter training for the base model selected
# by Sonder Runtime, performed on an explicitly provisioned cloud GPU.
#
# Where to run: any Linux box with an NVIDIA GPU + CUDA (RunPod, Lambda,
# vast.ai, Paperspace, a cloud VM with an A100/H100/4090/3090, etc.). A 24 GB
# card (e.g. RTX 4090/3090) comfortably QLoRA-tunes the 7B; 16 GB works with
# the smaller bases.
#
# Usage (on the GPU box):
#   git clone https://github.com/Krilliac/Sonder-runtime.git
#   cd Sonder-runtime
#   # upload your dataset (see step 3 below) then:
#   bash cloud_train.sh
#
# Env knobs (all optional):
#   SONDER_BASE       supported HF base model (default Qwen/Qwen2.5-Coder-7B-Instruct)
#   SONDER_LORA_OUT   run root (default ./sonder-personal-lora)
#   SONDER_TRAIN_GPU_INDEX physical CUDA GPU index (default 0)
#   HF_TOKEN          set to also push the adapter to the Hugging Face Hub
#   HF_REPO           e.g. Krilliac/sonder-personal-lora (needed with HF_TOKEN)
set -euo pipefail

BASE="${SONDER_BASE:-Qwen/Qwen2.5-Coder-7B-Instruct}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_ROOT="${SONDER_LORA_OUT:-$HERE/sonder-personal-lora}"
TRAINING_STATE="$HERE/.cloud-training-state.json"
case "$BASE" in
  Qwen/Qwen2.5-Coder-1.5B-Instruct) MODEL_SIZE="1.5b" ;;
  Qwen/Qwen2.5-Coder-3B-Instruct) MODEL_SIZE="3b" ;;
  Qwen/Qwen2.5-Coder-7B-Instruct) MODEL_SIZE="7b" ;;
  *)
    echo "ERROR: unsupported SONDER_BASE '$BASE'; choose the mapped 1.5B, 3B, or 7B Qwen2.5-Coder base." >&2
    exit 2
    ;;
esac
cd "$HERE"

echo "======================================================================"
echo " Sonder Runtime adapter-training helper"
echo " base model: $BASE"
echo "======================================================================"

echo "== 1/5 GPU check =="
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "ERROR: nvidia-smi not found — this needs a CUDA GPU box. Aborting." >&2
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true

echo "== 2/5 python venv + deps =="
if ! command -v python3 >/dev/null 2>&1; then
  (apt-get update -y && apt-get install -y python3 python3-venv python3-pip git) \
    || { echo "install python3/venv/pip/git manually" >&2; exit 1; }
fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel
# CUDA build of torch first (cu121 works on most current GPU images; change the
# index if your box has a different CUDA runtime).
python -m pip install torch --index-url https://download.pytorch.org/whl/cu121 \
  || python -m pip install torch   # fall back to default wheel if the index fails
python -m pip install -r requirements-train.txt

echo "== 3/5 dataset =="
if [ ! -f training_data.jsonl ]; then
  cat >&2 <<'MSG'
ERROR: training_data.jsonl not found in this directory.

Your training data lives on your LOCAL machine (it is gitignored on purpose —
it is derived from your own coding interactions). Get it onto this GPU box, e.g.
from your local machine:

  # regenerate it locally first if needed:
  ./venv/Scripts/python.exe export_training_data.py
  # then upload it to this box:
  scp training_data.jsonl user@THIS_BOX_IP:PATH/Sonder-runtime/

Then re-run:  bash cloud_train.sh
MSG
  exit 1
fi
N=$(wc -l < training_data.jsonl | tr -d ' ')
echo "found training_data.jsonl with $N examples"
if [ "$N" -lt 50 ]; then
  echo "NOTE: only $N examples — this will over/under-fit; it is a pipeline proof."
  echo "Grow the dataset first (open the sonder REPL and run /train) for a real tune."
fi

echo "== 4/5 QLoRA fine-tune (this is the long part) =="
export SONDER_DATA="$HERE/training_data.jsonl"
export SONDER_LORA_OUT="$OUTPUT_ROOT"
export SONDER_TRAINING_STATE="$TRAINING_STATE"
python adaptive_training.py start --confirm --model "$MODEL_SIZE" \
  --gpu-index "${SONDER_TRAIN_GPU_INDEX:-0}"
ADAPTER_DIR="$(python - "$TRAINING_STATE" <<'PY'
import json
import sys
from pathlib import Path

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if state.get("status") != "trained" or not state.get("adapter_dir"):
    raise SystemExit("training state did not record a trained adapter")
print(state["adapter_dir"])
PY
)"
RUN_ID="$(basename "$(dirname "$ADAPTER_DIR")")"
ADAPTER_NAME="sonder-personal-lora-$RUN_ID"
export SONDER_CLOUD_ADAPTER_DIR="$ADAPTER_DIR"

echo "== 5/5 package the adapter =="
if [ ! -d "$ADAPTER_DIR" ]; then
  echo "WARNING: $ADAPTER_DIR not produced — check the training log above." >&2
  exit 1
fi
TARBALL="${ADAPTER_NAME}.tar.gz"
tar -czf "$TARBALL" \
  --transform "s,^$(basename "$ADAPTER_DIR"),$ADAPTER_NAME," \
  -C "$(dirname "$ADAPTER_DIR")" "$(basename "$ADAPTER_DIR")"
echo "adapter packaged -> $HERE/$TARBALL ($(du -h "$TARBALL" | cut -f1))"

# Optional: push to the HF Hub if credentials are provided
if [ -n "${HF_TOKEN:-}" ] && [ -n "${HF_REPO:-}" ]; then
  echo "-- pushing adapter to HF Hub: $HF_REPO --"
  python -m pip install -q huggingface_hub
  python - <<PY
import os
from huggingface_hub import HfApi
api = HfApi(token=os.environ["HF_TOKEN"])
api.create_repo(os.environ["HF_REPO"], exist_ok=True)
api.upload_folder(folder_path=os.environ["SONDER_CLOUD_ADAPTER_DIR"], repo_id=os.environ["HF_REPO"])
print("pushed to", os.environ["HF_REPO"])
PY
fi

cat <<MSG

======================================================================
 DONE. LoRA adapter is in:  $ADAPTER_DIR/   (also $TARBALL)
======================================================================
Get it back to your machine:
  scp user@THIS_BOX_IP:$HERE/$TARBALL .

Then turn it into a runnable Ollama model locally. peft outputs a safetensors
adapter; Ollama's Modelfile ADAPTER wants a GGUF LoRA, so convert with llama.cpp:
  python llama.cpp/convert_lora_to_gguf.py $ADAPTER_NAME --outfile $ADAPTER_NAME.gguf
  # Modelfile:
  #   FROM $BASE-equivalent-gguf   (or your base ollama model)
  #   ADAPTER ./$ADAPTER_NAME.gguf
  ollama create sonder-personal -f Modelfile
See TRAINING.md in this repo for the full, honest walkthrough + caveats.
MSG
