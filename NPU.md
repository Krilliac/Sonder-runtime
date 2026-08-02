# NPU utility accelerator

Sonder can use a local NPU as a **utility accelerator below** the existing
`fast` / `code` / `general` local model tiers. It is never a fourth generative
tier: it only pre-scores the ambiguous execution-routing band and (optionally)
serves embeddings for the exact configured vector space. Every accelerator
failure falls back to the existing local behavior. Cloud is never a fallback,
and the accelerator cannot see or change cloud access, permissions, roots,
credentials, or executable paths.

Default state is **off** and fully backward compatible: with no policy change
and no manifests, nothing spawns and nothing behaves differently.

## Architecture

```text
Sonder server (stdlib-only host path)
  runtime_policy.npu  -> off | shadow | prefer   (per capability)
  npu_service         -> policy gate, host validation, provenance, telemetry
  npu_broker          -> lazy spawn, single flight, deadlines, restart-once,
                         circuit breaker, RAM gate, RSS cap, idle unload
        |  bounded JSONL over stdio (1 MiB/line, strict JSON, no NaN)
        v
  npu_worker (restartable child process)
      all vendor imports live here: onnxruntime, tokenizers, numpy
      providers: vitisai | openvino | qnn | winml | cpu | cpu-sim
```

- The main server process never imports vendor runtimes for this path. The
  worker re-points its real stdout at stderr at startup so a chatty vendor DLL
  can never corrupt the protocol stream; any malformed or oversized protocol
  line poisons the worker and the broker kills it.
- Operational limits: one in-flight call; per-operation deadlines
  (routing ≤ 2 s, embeddings ≤ 10 s, defaults 400 ms / 2 s); request lines
  ≤ 1 MiB; ≤ 16 texts × 8000 chars; vectors ≤ 4096 dims; available-RAM spawn
  gate; worker RSS eviction cap; idle unload; restart-once then a circuit
  breaker with cooldown and a half-open probe.

## Providers (discovered, never assumed)

Capability is discovered per process by the worker and reported honestly as
`detected` (silicon/EP present) vs `runtime_ready` (a session can be created
now). The runtime records the execution provider the session actually got and
flags `ep_fallback` when it differs from the manifest's first choice.

| id        | Execution provider           | Notes |
|-----------|------------------------------|-------|
| `vitisai` | `VitisAIExecutionProvider`   | AMD Ryzen AI; requires AMD's onnxruntime build/SDK |
| `openvino`| `OpenVINOExecutionProvider`  | Intel NPU via `device_type=NPU` (set automatically) |
| `qnn`     | `QNNExecutionProvider`       | Qualcomm Hexagon; `provider_options.qnn.backend_path` may name the vendor backend |
| `winml`   | —                            | Descriptor only: no supported Python runtime path today. DirectML is GPU-class and is deliberately **not** claimed as an NPU |
| `cpu`     | `CPUExecutionProvider`       | onnxruntime CPU reference — the only allowed same-model fallback |
| `cpu-sim` | stdlib simulator             | Deterministic, dependency-free; used by CI and explicit opt-in testing; always reported as `simulated` |

If the runtime silently reassigns a session to CPU and the manifest does not
allowlist `cpu`, the load is refused rather than misreported as NPU.

## Model bundles (manifests)

Sonder never downloads or redistributes models or vendor SDKs. You provision
files yourself and describe them with a JSON manifest in the manifest
directory (`<sonder state home>/npu-manifests`, override with
`SONDER_NPU_MANIFEST_DIR`). File paths are relative to that directory —
manifests are portable and carry no absolute paths. Hash or size drift
disables a bundle instead of serving different weights.

```json
{
  "schema": 1,
  "name": "exec-route-v1",
  "operation": "routing",
  "model": {"path": "route.onnx", "sha256": "<64 hex>", "bytes": 12345},
  "input": {"identity": "exec-route-features-v1", "dimension": 16},
  "labels": ["workbench", "autopilot"],
  "postprocess": "softmax",
  "providers": ["vitisai", "cpu"],
  "limits": {"deadline_ms": 400}
}
```

```json
{
  "schema": 1,
  "name": "embed-npu-v1",
  "operation": "embedding",
  "model": {"path": "embedder.onnx", "sha256": "<64 hex>", "bytes": 123456},
  "tokenizer": {"type": "hf-tokenizers", "path": "tokenizer.json",
                "sha256": "<64 hex>", "bytes": 4567},
  "dimension": 768,
  "pooling": "mean",
  "normalize": true,
  "preprocess": "hf-tokenizer",
  "postprocess": "l2norm",
  "providers": ["openvino", "cpu"],
  "space": {"model": "nomic-embed-text:latest",
            "revision": "ollama-manifest-sha256:<64 hex>"},
  "limits": {"deadline_ms": 2000, "max_batch": 8, "max_text_chars": 4000}
}
```

`providers` is a priority-ordered allowlist. `limits` are clamped to the
global caps. One valid manifest per operation is active (lexicographically
first by name).

## Policy: off, shadow, prefer

The shared runtime policy gains an `npu` section (`runtime_policy_status`
shows it; `runtime_policy_update` edits it):

```
runtime_policy_update npu_json='{"mode": "shadow", "routing": "prefer"}'
```

- `off` (default): the accelerator is never consulted; nothing spawns.
- `shadow`: the accelerator runs beside the existing path; results are
  discarded, only agreement/latency/health telemetry is recorded. Behavior
  never changes (it can add bounded latency — that is shadow's cost).
- `prefer`: the accelerator result is used when it validates; any miss falls
  back to the existing local path.

`routing`/`embeddings` override the mode per capability. The policy section
carries **only** these mode strings — models, paths, providers, limits, and
credentials are not policy-expressible, and legacy policy files without the
section normalize to `off`.

## Capability 1: ambiguous execution routing

Deterministic host cues (explicit fleet/autopilot/foreground requests) remain
authoritative and never reach the accelerator. Only the ambiguous "decide"
band consults it:

- The host computes a versioned, bounded numeric feature vector
  (`exec-route-features-v1`); raw prompt text never crosses the process
  boundary for routing.
- The accelerator may return only scores over `workbench`/`autopilot` plus an
  allowlisted `reason_code`. The host validates labels, finiteness, ranges,
  and reason codes; anything else is discarded.
- `prefer` uses a confidently scored winner (≥ 0.6) and otherwise falls back
  to the existing local Ollama router (and its own Autopilot fallback).
- `shadow` records agreement with the baseline decision and changes nothing.

## Capability 2: embeddings

`embeddings.embed()` keeps its exact legacy contract (including soft-fail to
None and the callers' lexical fallback). New typed path:

- `embeddings.embed_result(text)` returns the vector plus provenance:
  `model`, `revision`, `dimension`, `provider` (`ollama` or `npu:<id>`),
  `accelerated`, `simulated`, `fallback_reason`.
- Acceleration happens only when policy prefers embeddings **and** the active
  embedding manifest declares `space` pinning the exact model identity and
  serving revision the legacy embedder would use right now. That declaration
  asserts identical weights/tokenizer/pooling/normalization; the revision pin
  means a retagged Ollama model or drifted ONNX file disables acceleration.
- Same-model CPU fallback (the `cpu` provider on the same pinned ONNX) is the
  only allowed in-worker fallback. Vector spaces never mix, and a different
  embedder is never silently substituted — a legacy fallback is visible in
  `fallback_reason` and activity telemetry.

## Diagnostics and telemetry

- `npu_status` (tool): detected vs runtime-ready vs enabled vs healthy,
  provider table, model bundle hashes, worker/circuit state, bounded latency
  percentiles, fallback counters, shadow agreement.
- `diagnostics` / `status` include a one-line accelerator summary;
  `runtime_policy_status` shows the policy modes; the hardware profile
  reports `npu_vendor`/`npu_name`/`npu_detected`.
- Activity records bounded events (`npu_route`, `npu_route_shadow`,
  `npu_embed`, `npu_fallback`, …) with enums and counts only. Prompt text,
  vectors, and logits are never logged anywhere.

## Optional dependencies

Nothing is required for the default-off state or for CI (the `cpu-sim`
provider is stdlib-only). For real execution, install into the worker's
Python environment:

- `pip install onnxruntime` — CPU reference provider (`cpu`).
- AMD Ryzen AI (`vitisai`), Intel OpenVINO (`openvino`), Qualcomm QNN
  (`qnn`): install the vendor's onnxruntime build/SDK per their
  documentation. Sonder does not fetch or bundle these.
- `pip install tokenizers` — required for real (non-simulated) embedding
  bundles with an `hf-tokenizers` tokenizer.

Environment knobs (all optional): `SONDER_NPU_MANIFEST_DIR`,
`SONDER_NPU_MIN_FREE_RAM_GB` (default 2), `SONDER_NPU_MAX_RSS_MB` (default
1536), `SONDER_NPU_IDLE_UNLOAD_S` (default 300),
`SONDER_NPU_CIRCUIT_COOLDOWN_S` (default 120). `SONDER_NPU_TEST_HOOKS=1`
enables test-only worker hooks and is never set in production.
