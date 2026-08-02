"""Distill Sonder's local execution router into a tiny ONNX routing model.

The NPU utility accelerator pre-scores only the ambiguous "decide" band of
execution routing (see NPU.md).  This script builds the routing bundle that
capability consumes:

1. Generates a deterministic corpus of compound work prompts that land in the
   ambiguous band (``intents.classify_execution -> mode "decide"``).
2. Labels each prompt with the real baseline: ``server._execution_route_model``
   running against the local Ollama router tier (temperature 0).  Labels are
   cached to a JSONL sidecar so interrupted runs resume without re-querying.
3. Trains a small numpy MLP on the current default feature contract
   (``npu_service.FEATURES_ID``) to reproduce the baseline decision.
4. Exports the network as a self-contained ONNX file and writes the pinned
   ``exec-route-v1`` manifest beside it.

The output model is a distillation of the existing local router, not a new
policy: shadow mode measures live agreement with the same baseline, and
``prefer`` only uses confidently scored winners (>= 0.6) with full fallback.

Usage (from the repo root, inside the runtime venv):

    python scripts/npu_distill_router.py --samples 480
    python scripts/npu_distill_router.py --samples 480 --min-agreement 0.85

Requires: the Ollama runtime serving the configured router tier, plus the
``onnx`` package for export.  Distillation writes only under the manifest
directory (``SONDER_NPU_MANIFEST_DIR`` or ``<state>/npu-manifests``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Verbs/targets mirror intents._WORK_ACTION_RE / _WORK_TARGET_RE so generated
# prompts classify as work.  Explicit autopilot/fleet/foreground cue words are
# deliberately absent: those bands never reach the accelerator.
_VERBS = [
    "audit", "benchmark", "build", "create", "diagnose", "document", "edit",
    "find", "fix", "generate", "implement", "inspect", "install", "list",
    "refactor", "rename", "repair", "review", "run", "scan", "scaffold",
    "search", "test", "update", "validate", "verify", "write",
]
_TARGETS = [
    "api", "app", "build", "cli", "config", "dashboard", "docs", "file",
    "function", "library", "model", "package", "project", "readme", "repo",
    "report", "script", "system", "tests", "tool", "ui", "workspace",
]
_QUALIFIERS = [
    "in ./src", "under the tools directory", "for the auth module",
    "in the main package", "across the whole repo", "for release",
    "in config.yaml", "before the demo", "in the ci pipeline",
    "for the storage layer", "in ./app", "on the settings page",
]
_CONNECTORS = ["then", "after that", "next", "finally", "and then"]
_TAILS = [
    "make sure nothing regresses.",
    "double-check the edge cases as you go.",
    "keep the diff small.",
    "flag anything suspicious you notice.",
    "confirm the results look right.",
    "capture what changed for the changelog.",
]


_SCOPES = ["all", "every", "each of", "the entire set of", "both", "several"]
_DETAILS = [
    "there are 3 known failures", "it touches 12 files",
    "target the v2 endpoints", "budget about 45 minutes",
    "the last run took 8 minutes", "two of the 5 checks are red",
]
_MARKERS = [
    "plan the ordering before touching anything",
    "test the result after each step",
    "inspect the current behavior first",
    "verify whether the old behavior still matters",
    "check the diff before finishing",
]


def _phrase(rng: random.Random) -> str:
    verb = rng.choice(_VERBS)
    target = rng.choice(_TARGETS)
    roll = rng.random()
    if roll < 0.35:
        return "%s the %s %s" % (verb, target, rng.choice(_QUALIFIERS))
    if roll < 0.5:
        return "%s %s the %s" % (verb, rng.choice(_SCOPES), target + "s"
                                 if not target.endswith("s") else target)
    return "%s the %s" % (verb, target)


def _compose(rng: random.Random) -> str:
    """One prompt drawn from structural archetypes spanning the feature space.

    The archetypes deliberately vary length, phase count, sentence count,
    digits, quantifiers, and plan/test/inspect markers so the bounded feature
    vector actually separates small bounded compounds from heavily phased
    work — a homogeneous template corpus pins those features near-constant
    and caps distillation agreement at the majority rate.
    """
    archetype = rng.random()
    if archetype < 0.3:
        # Small bounded compound: three quick actions, one line.
        parts = [_phrase(rng) for _ in range(3)]
        text = "%s, %s, %s %s" % (
            parts[0], parts[1], rng.choice(_CONNECTORS), parts[2])
        if rng.random() < 0.3:
            text += ", " + rng.choice(_DETAILS)
    elif archetype < 0.5:
        # Mid-size sequenced work, single sentence run-on.
        parts = [_phrase(rng) for _ in range(rng.choice((3, 4)))]
        glue = " %s " % rng.choice(_CONNECTORS)
        text = glue.join(parts)
        if rng.random() < 0.5:
            text += " because " + rng.choice(_DETAILS)
    elif archetype < 0.8:
        # Phased project: multiple sentences, markers, scope words.
        phases = rng.choice((4, 5, 6, 7))
        sentences = [_phrase(rng).capitalize() for _ in range(phases)]
        if rng.random() < 0.7:
            sentences.insert(
                rng.randrange(len(sentences)),
                rng.choice(_MARKERS).capitalize())
        if rng.random() < 0.6:
            sentences.append(rng.choice(_DETAILS).capitalize())
        text = ". ".join(sentences) + "."
        if rng.random() < 0.5:
            text = text.rstrip(".") + ". " + rng.choice(_TAILS).capitalize()
    else:
        # Numbered multi-stage request.
        phases = rng.choice((3, 4, 5))
        steps = [
            "%d) %s" % (index + 1, _phrase(rng))
            for index in range(phases)
        ]
        text = "Work through the %s: %s. %s" % (
            rng.choice(("backlog", "checklist", "cleanup list")),
            "; ".join(steps), rng.choice(_TAILS).capitalize())
    return text[0].upper() + text[1:]


def generate_corpus(samples: int, seed: int) -> list:
    """Deterministic compound prompts spanning small to heavily phased work."""
    rng = random.Random(seed)
    prompts = []
    seen = set()
    attempts = 0
    while len(prompts) < samples and attempts < samples * 40:
        attempts += 1
        text = _compose(rng)
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        prompts.append(text)
    return prompts


def filter_decide_band(prompts: list) -> list:
    import intents

    kept = []
    for prompt in prompts:
        decision = intents.classify_execution(prompt)
        if decision and decision.get("mode") == "decide":
            kept.append(prompt)
    return kept


def _cache_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]


def load_cache(path: Path) -> dict:
    rows = {}
    if path.is_file():
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    rows[row["key"]] = row
                except (KeyError, TypeError, ValueError):
                    continue
    return rows


def label_prompts(prompts: list, cache_path: Path, quiet: bool) -> list:
    """Label via the real baseline router; cache results between runs."""
    import server  # heavy import: the runtime module hosting the baseline

    cache = load_cache(cache_path)
    labeled = []
    fresh = 0
    with cache_path.open("a", encoding="utf-8") as sink:
        for index, prompt in enumerate(prompts):
            key = _cache_key(prompt)
            row = cache.get(key)
            if row is None:
                try:
                    routed = server._execution_route_model(prompt)
                except Exception as exc:
                    print("  label %d/%d failed: %s"
                          % (index + 1, len(prompts), str(exc)[:120]))
                    continue
                row = {
                    "key": key,
                    "prompt": prompt,
                    "mode": routed["mode"],
                    "tier": routed.get("tier", ""),
                    "confidence": routed.get("confidence"),
                }
                sink.write(json.dumps(row, ensure_ascii=True) + "\n")
                sink.flush()
                fresh += 1
                if not quiet and fresh % 20 == 0:
                    print("  labeled %d fresh (%d/%d total)"
                          % (fresh, index + 1, len(prompts)))
            labeled.append(row)
    return labeled


# Output order is the manifest ``labels`` order; keep the two in lockstep.
LABELS = ("workbench", "autopilot")


def train_mlp(features, targets, seed: int, hidden: int = 24):
    """Train a 16 -> hidden -> 2 softmax MLP with plain numpy Adam."""
    import numpy as np

    rng = np.random.default_rng(seed)
    x = np.asarray(features, dtype=np.float64)
    y = np.asarray(targets, dtype=np.int64)
    order = rng.permutation(len(x))
    x, y = x[order], y[order]
    split = max(1, int(len(x) * 0.8))
    x_train, y_train = x[:split], y[:split]
    x_hold, y_hold = x[split:], y[split:]

    dim = x.shape[1]
    w1 = rng.normal(0.0, 0.35, (dim, hidden))
    b1 = np.zeros(hidden)
    w2 = rng.normal(0.0, 0.35, (hidden, len(LABELS)))
    b2 = np.zeros(len(LABELS))
    params = [w1, b1, w2, b2]
    moments = [np.zeros_like(p) for p in params]
    velocities = [np.zeros_like(p) for p in params]
    counts = np.bincount(y_train, minlength=len(LABELS)).astype(np.float64)
    class_weight = counts.sum() / np.clip(counts * len(LABELS), 1.0, None)

    def forward(batch):
        hidden_pre = batch @ params[0] + params[1]
        hidden_act = np.maximum(hidden_pre, 0.0)
        logits = hidden_act @ params[2] + params[3]
        return hidden_pre, hidden_act, logits

    def accuracy(bx, by):
        if not len(bx):
            return float("nan")
        return float((forward(bx)[2].argmax(axis=1) == by).mean())

    lr, beta1, beta2, eps, l2 = 0.01, 0.9, 0.999, 1e-8, 1e-4
    step = 0
    for epoch in range(400):
        batch_order = rng.permutation(len(x_train))
        for start in range(0, len(x_train), 32):
            rows = batch_order[start:start + 32]
            bx, by = x_train[rows], y_train[rows]
            hidden_pre, hidden_act, logits = forward(bx)
            shifted = logits - logits.max(axis=1, keepdims=True)
            probs = np.exp(shifted)
            probs /= probs.sum(axis=1, keepdims=True)
            grad_logits = probs.copy()
            grad_logits[np.arange(len(by)), by] -= 1.0
            grad_logits *= class_weight[by][:, None] / len(by)
            grads = [
                bx.T @ (grad_logits @ params[2].T * (hidden_pre > 0.0)),
                (grad_logits @ params[2].T * (hidden_pre > 0.0)).sum(axis=0),
                hidden_act.T @ grad_logits,
                grad_logits.sum(axis=0),
            ]
            step += 1
            for i, (param, grad) in enumerate(zip(params, grads)):
                grad = grad + l2 * param
                moments[i] = beta1 * moments[i] + (1 - beta1) * grad
                velocities[i] = beta2 * velocities[i] + (1 - beta2) * grad ** 2
                m_hat = moments[i] / (1 - beta1 ** step)
                v_hat = velocities[i] / (1 - beta2 ** step)
                param -= lr * m_hat / (np.sqrt(v_hat) + eps)
    stats = {
        "train_n": int(len(x_train)),
        "holdout_n": int(len(x_hold)),
        "train_acc": accuracy(x_train, y_train),
        "holdout_acc": accuracy(x_hold, y_hold),
        "majority": float(max(counts) / max(counts.sum(), 1.0)),
        "class_counts": {
            LABELS[i]: int(c) for i, c in enumerate(counts)
        },
    }
    return [p.astype("float32") for p in params], stats


def export_onnx(params, out_path: Path, features_dim: int) -> bytes:
    import onnx
    from onnx import TensorProto, helper

    w1, b1, w2, b2 = params
    graph = helper.make_graph(
        [
            helper.make_node("MatMul", ["features", "w1"], ["h0"]),
            helper.make_node("Add", ["h0", "b1"], ["h1"]),
            helper.make_node("Relu", ["h1"], ["h2"]),
            helper.make_node("MatMul", ["h2", "w2"], ["h3"]),
            helper.make_node("Add", ["h3", "b2"], ["logits"]),
        ],
        "sonder-exec-route-distilled",
        [helper.make_tensor_value_info(
            "features", TensorProto.FLOAT, [1, features_dim])],
        [helper.make_tensor_value_info(
            "logits", TensorProto.FLOAT, [1, len(LABELS)])],
        initializer=[
            helper.make_tensor(
                name, TensorProto.FLOAT, list(array.shape), array.flatten())
            for name, array in
            (("w1", w1), ("b1", b1), ("w2", w2), ("b2", b2))
        ],
    )
    model = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    onnx.checker.check_model(model)
    payload = model.SerializeToString()
    out_path.write_bytes(payload)
    return payload


def write_manifest(manifest_dir: Path, model_rel: str, payload: bytes,
                   features_id: str, dimension: int) -> Path:
    manifest = {
        "schema": 1,
        "name": "exec-route-v1",
        "operation": "routing",
        "model": {
            "path": model_rel,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        "input": {"identity": features_id, "dimension": dimension},
        "labels": list(LABELS),
        "postprocess": "softmax",
        "providers": ["vitisai", "cpu"],
        "limits": {"deadline_ms": 400},
    }
    path = manifest_dir / "exec-route-v1.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", type=int, default=480)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--manifest-dir", default="")
    parser.add_argument("--min-agreement", type=float, default=0.8,
                        help="refuse to write the manifest below this "
                             "holdout agreement with the baseline router")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    import npu_manifest
    import npu_service

    manifest_dir = Path(args.manifest_dir or npu_manifest.manifest_dir())
    models_dir = manifest_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    cache_path = models_dir / "router-distill-cache.jsonl"

    started = time.time()
    corpus = filter_decide_band(generate_corpus(args.samples, args.seed))
    print("corpus: %d prompts in the ambiguous decide band" % len(corpus))
    labeled = label_prompts(corpus, cache_path, args.quiet)
    usable = [row for row in labeled if row.get("mode") in LABELS]
    print("labels: %d usable baseline decisions (cache: %s)"
          % (len(usable), cache_path))
    if len(usable) < 60:
        print("not enough labeled samples to distill; is Ollama serving the "
              "router tier?")
        return 1

    features_dim, feature_fn = npu_service.FEATURE_CONTRACTS[
        npu_service.FEATURES_ID]
    features = [feature_fn(row["prompt"]) for row in usable]
    targets = [LABELS.index(row["mode"]) for row in usable]
    params, stats = train_mlp(features, targets, args.seed)
    print("training: %(train_n)d train / %(holdout_n)d holdout | "
          "train_acc=%(train_acc).3f holdout_acc=%(holdout_acc).3f "
          "majority=%(majority).3f" % stats)
    print("class counts: %s" % stats["class_counts"])

    if stats["holdout_n"] and stats["holdout_acc"] < args.min_agreement:
        print("holdout agreement below --min-agreement %.2f; manifest not "
              "written" % args.min_agreement)
        return 1

    payload = export_onnx(params, models_dir / "route.onnx", features_dim)
    manifest_path = write_manifest(
        manifest_dir, "models/route.onnx", payload,
        npu_service.FEATURES_ID, features_dim)
    report = {
        "generated_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "samples": len(usable),
        "seed": args.seed,
        "features_id": npu_service.FEATURES_ID,
        "stats": stats,
        "model_sha256": hashlib.sha256(payload).hexdigest(),
        "elapsed_s": round(time.time() - started, 1),
    }
    (models_dir / "router-training-report.txt").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("wrote %s (%d bytes model)" % (manifest_path, len(payload)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
