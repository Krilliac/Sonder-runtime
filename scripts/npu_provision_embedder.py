"""Provision the NPU embedding bundle for the exact configured Ollama space.

Downloads the operator-chosen ONNX export of the embedding model (default:
nomic-ai/nomic-embed-text-v1.5, matching Ollama's ``nomic-embed-text``),
verifies it is numerically equivalent to the live Ollama embedder (cosine
similarity on probe texts), pins every byte, and writes the ``embed-npu-v1``
manifest.  Sonder itself never downloads models; this script is the operator
action NPU.md describes, automated and verified.

The manifest's ``space`` section pins the current local Ollama manifest
revision, so a retagged/updated Ollama model automatically disables
acceleration until this script is re-run against the new revision.

Usage (from the repo root, inside the runtime venv, Ollama serving):

    python scripts/npu_provision_embedder.py
    python scripts/npu_provision_embedder.py --min-cosine 0.999 --skip-download

Requires network access for the one-time download plus ``onnxruntime`` and
``tokenizers`` in the worker environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_DEFAULT_REPO = "nomic-ai/nomic-embed-text-v1.5"
_PROBES = [
    "Fix the failing unit test in the storage layer and rerun the suite.",
    "What is the capital of France?",
    "refactor the api handlers, update the docs, then validate the build",
    "The quick brown fox jumps over the lazy dog.",
    "sonder npu accelerator shadow-mode agreement telemetry",
    "Summarize the deployment checklist for the release branch.",
]


def _download(url: str, dest: Path) -> None:
    print("downloading %s" % url)
    request = urllib.request.Request(url, headers={"User-Agent": "sonder-npu-provision"})
    with urllib.request.urlopen(request, timeout=120) as response, \
            dest.open("wb") as sink:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            sink.write(chunk)
    print("  -> %s (%d bytes)" % (dest, dest.stat().st_size))


def _pin(base: Path, rel: str) -> dict:
    data = (base / rel).read_bytes()
    return {
        "path": rel,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _onnx_embed(session, tokenizer, text):
    import numpy as np

    encoding = tokenizer.encode(text)
    feeds = {
        "input_ids": np.asarray([encoding.ids], dtype=np.int64),
        "attention_mask": np.asarray([encoding.attention_mask], dtype=np.int64),
        "token_type_ids": np.asarray([encoding.type_ids], dtype=np.int64),
    }
    feeds = {
        name: value for name, value in feeds.items()
        if name in {meta.name for meta in session.get_inputs()}
    }
    hidden = session.run(None, feeds)[0].astype(np.float64)
    mask = np.asarray([encoding.attention_mask], dtype=np.float64)[:, :, None]
    pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1.0, None)
    pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
    return pooled[0]


def verify_equivalence(models_dir: Path, min_cosine: float) -> float:
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    import embeddings

    tokenizer = Tokenizer.from_file(str(models_dir / "tokenizer.json"))
    if getattr(tokenizer, "truncation", None) or getattr(tokenizer, "padding", None):
        raise SystemExit("tokenizer.json must not enable truncation or padding")
    session = ort.InferenceSession(
        str(models_dir / "embedder.onnx"), providers=["CPUExecutionProvider"],
    )
    worst = 1.0
    for text in _PROBES:
        reference = embeddings.embed(text)
        if reference is None:
            raise SystemExit(
                "Ollama embedder unavailable; start Ollama before provisioning"
            )
        reference = np.asarray(reference, dtype=np.float64)
        reference /= np.linalg.norm(reference)
        candidate = _onnx_embed(session, tokenizer, text)
        cosine = float(reference @ candidate)
        worst = min(worst, cosine)
        print("  cos=%.6f  %s" % (cosine, text[:56]))
    if worst < min_cosine:
        raise SystemExit(
            "equivalence check failed: worst cosine %.6f < %.3f — the ONNX "
            "export does not match the live Ollama space" % (worst, min_cosine)
        )
    return worst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hf-repo", default=_DEFAULT_REPO)
    parser.add_argument("--manifest-dir", default="")
    parser.add_argument("--min-cosine", type=float, default=0.999)
    parser.add_argument("--skip-download", action="store_true",
                        help="verify and pin already-downloaded files only")
    args = parser.parse_args()

    import embeddings
    import npu_manifest

    manifest_dir = Path(args.manifest_dir or npu_manifest.manifest_dir())
    models_dir = manifest_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_download:
        root = "https://huggingface.co/%s/resolve/main" % args.hf_repo
        _download(root + "/onnx/model.onnx", models_dir / "embedder.onnx")
        _download(root + "/tokenizer.json", models_dir / "tokenizer.json")

    identity = embeddings.EMBED_IDENTITY
    revision = embeddings.current_revision()
    dimension = 768
    if not revision:
        raise SystemExit("could not resolve the local Ollama manifest revision")
    print("space: %s @ %s" % (identity, revision))
    worst = verify_equivalence(models_dir, args.min_cosine)

    manifest = {
        "schema": 1,
        "name": "embed-npu-v1",
        "operation": "embedding",
        "model": _pin(manifest_dir, "models/embedder.onnx"),
        "tokenizer": {
            "type": "hf-tokenizers",
            **_pin(manifest_dir, "models/tokenizer.json"),
        },
        "dimension": dimension,
        "pooling": "mean",
        "normalize": True,
        "preprocess": "hf-tokenizer",
        "postprocess": "l2norm",
        "providers": ["vitisai", "cpu"],
        "space": {
            "model": identity,
            "revision": revision,
            "dimension": dimension,
        },
        "limits": {"deadline_ms": 2000, "max_batch": 8, "max_text_chars": 4000},
    }
    path = manifest_dir / "embed-npu-v1.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    rows = npu_manifest.load_manifests(manifest_dir)
    errors = [row["error"] for row in rows if row.get("error")]
    if errors:
        raise SystemExit("manifest failed validation: %s" % errors[0])
    print("wrote %s (worst probe cosine %.6f)" % (path, worst))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
