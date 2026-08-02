# NPU utility accelerator

Sonder can use a local NPU as a **utility accelerator below** the existing
`fast` / `code` / `general` local model tiers. It is never a fourth generative
tier: it only pre-scores the ambiguous execution-routing band and (optionally)
serves embeddings for the exact configured vector space. Every accelerator
failure falls back to the existing local behavior. Cloud is never a fallback,
and the protocol has no operations for cloud access, permissions, roots, or
credentials. The worker environment is explicitly allowlisted and excludes
cloud/API credential namespaces and the parent Python import path.

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
      providers: vitisai | openvino | qnn | winml | cpu
      test-only provider: cpu-sim (requires SONDER_NPU_TEST_HOOKS=1)
```

- The main server process never imports vendor runtimes for this path. The
  worker re-points its real stdout at stderr at startup so a chatty vendor DLL
  can never corrupt the protocol stream; any malformed or oversized protocol
  line poisons the worker and the broker kills it.
- The worker is failure isolation, **not an OS security sandbox**. It runs as
  the same user and can access whatever that account can access. Vendor
  runtimes and model bundles remain trusted local inputs. Its inherited
  environment is reduced to OS/Python loader variables, explicitly named
  non-credential NPU-vendor runtime variables, and exact test-hook controls;
  prefix-matched vendor variables are not inherited. Ordinary cloud/API keys
  and unrelated `SONDER_*` settings are not inherited either.
- Worker teardown contains descendants with a Windows kill-on-close Job Object
  (exact-PID tree fallback when Job assignment is unavailable) or a POSIX
  process group. It never targets unrelated processes.
- Operational limits: one in-flight call; vendor-exchange deadlines
  (routing ≤ 2 s, embeddings ≤ 10 s, defaults 400 ms / 2 s); request lines
  ≤ 1 MiB; ≤ 16 texts × 8000 chars; vectors ≤ 4096 dims; available-RAM spawn
  gate; pinned files ≤ 768 MiB and the combined loaded bundle ≤ 1 GiB; worker
  RSS is checked at ready/hello/detect/load/error/success; idle unload;
  restart-once then a circuit breaker with cooldown and a half-open probe.
  Complete pinned-file hashing happens during session load. Inference uses a
  cheap metadata-fingerprint check against the immutable loaded/staged session,
  avoiding repeated bundle I/O on the CPU hot path.

## Providers (discovered, never assumed)

`detected` comes from a cached, NPU-only host hardware discovery probe, not an
EP name or session claim. Provider-table detection is mapped from that host
vendor fact; the worker cannot manufacture it.
`registered` means only that the worker can see an execution provider;
`utility_ready` is promoted after any compatible allowlisted model session has
actually loaded. The global `runtime_ready` flag is narrower: it requires a
successful session with explicit NPU-device attestation, so CPU, CPU simulator,
and target-unverified VitisAI sessions cannot make the NPU status look ready.
NPU sessions disable ONNX Runtime's implicit CPU EP fallback and must
bind exclusively to the requested execution provider. Failed session creation
or provider introspection tries the next allowlisted provider as a separate
session; `ep_fallback` records that choice.

| id        | Execution provider           | Notes |
|-----------|------------------------------|-------|
| `vitisai` | `VitisAIExecutionProvider`   | AMD Vitis AI spans Ryzen AI, adaptable SoCs, and Alveo. An exclusive successful session is usable, but is reported as an unverified utility sidecar—not NPU-accelerated—until the runtime exposes effective-target attestation. An optional `config_file` must be a pinned `extra_file`. |
| `openvino`| `OpenVINOExecutionProvider`  | Intel NPU only; `device_type` is fixed to `NPU` and CPU/GPU values are rejected. |
| `qnn`     | `QNNExecutionProvider`       | Qualcomm HTP/NPU only; `backend_path` is required, must name a QNN HTP backend, and must reference a pinned `extra_file`. CPU/GPU QNN backends are rejected. |
| `winml`   | —                            | Descriptor only: no supported Python runtime path today and never reported detected/ready. DirectML is GPU-class and is deliberately **not** claimed as an NPU. |
| `cpu`     | `CPUExecutionProvider`       | onnxruntime CPU reference — the only allowed same-model fallback; never reported as NPU acceleration |
| `cpu-sim` | stdlib simulator             | Deterministic CI/test provider. It is registered only under `SONDER_NPU_TEST_HOOKS=1`, always reports `simulated`, and can never replace a production embedding vector. |

If an NPU session exposes CPU or any other extra provider, load is refused.
An allowlisted `cpu` fallback is created independently and is therefore
reported as CPU reference execution, never as NPU acceleration.

## Model bundles (manifests)

Sonder never downloads or redistributes models or vendor SDKs. You provision
files yourself and describe them with a JSON manifest in the manifest
directory (`<sonder state home>/npu-manifests`, override with
`SONDER_NPU_MANIFEST_DIR`). File paths are relative to that directory —
manifests are portable and carry no absolute paths. Vendor file options must
reference entries in `extra_files`; they cannot introduce unpinned absolute or
traversal paths. Hash or size drift
disables a bundle instead of serving different weights.

The worker hashes the exact model/tokenizer bytes it passes to the vendor
runtime, and file-valued vendor assets are copied into a private read-only
per-worker snapshot. The broker records dev/inode/size/mtime fingerprints after
that full load-time verification and compares them before inference. Ordinary
source drift fails closed and requires a fresh load; a same-user
metadata-preserving edit
cannot alter the already loaded bytes and is re-hashed on the next load. This
runtime is failure isolation, not a same-user security sandbox.
ONNX models with externally stored tensor data are not supported in v1; use a
self-contained ONNX file so the loaded bytes can be bound to the manifest.

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
            "revision": "ollama-manifest-sha256:<64 hex>",
            "dimension": 768},
  "limits": {"deadline_ms": 2000, "max_batch": 8, "max_text_chars": 4000}
}
```

`providers` is a priority-ordered allowlist. If a registered provider cannot
create the session, the worker tries the next runtime-ready allowlisted
provider; any fallback is reported explicitly. `limits` are clamped to the
global caps. One valid manifest per operation is active (lexicographically
first by name).

File-valued provider options use a relative reference to a hash-pinned
`extra_files` entry, for example:

```json
{
  "extra_files": [
    {"path": "QnnHtp.dll", "sha256": "<64 hex>", "bytes": 123456}
  ],
  "provider_options": {"qnn": {"backend_path": "QnnHtp.dll"}}
}
```

Embedding bundle ABI v1 requires an exact `input_ids` ONNX input and permits
only optional `attention_mask` and `token_type_ids` inputs. Unknown inputs fail
closed. The first output must be either a 2-D pooled tensor or a 3-D hidden-state
tensor handled by the declared pooling mode. `tokenizer.json` must not enable
automatic truncation or padding; runtime-detected overflow also fails closed.

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
  `model`, `revision`, `dimension`, `provider` (`ollama`, `npu:<id>`,
  `cpu-reference`, or `cpu-sim`), `accelerated`, `simulated`,
  `fallback_reason`. CPU reference/simulator results always report
  `accelerated=false`.
- Acceleration happens only when policy prefers embeddings **and** the active
  embedding manifest declares `space` pinning the exact model identity and
  serving revision and expected dimension the legacy embedder would use right
  now. The accelerator adds no further silent truncation: text above the
  manifest limit falls back intact from the NPU path. The established legacy
  `EMBED_MAX_CHARS` context cap still runs before either embedding path.
  Simulated output never substitutes. That declaration
  asserts identical weights/tokenizer/pooling/normalization; the revision pin
  means a retagged Ollama model or drifted ONNX file disables acceleration.
- Same-model CPU fallback (the `cpu` provider on the same pinned ONNX) is the
  only allowed in-worker fallback. Vector spaces never mix, and a different
  embedder is never silently substituted — a legacy fallback is visible in
  `fallback_reason` and activity telemetry. Target-unverified VitisAI results
  may be observed in shadow/routing work but never substitute a production
  embedding vector.
- Every load performs complete bounded size/SHA-256 verification of all pinned
  files. Each inference performs a cheap source fingerprint drift check against
  the immutable loaded/staged session; drift marks the manifest unhealthy.
- ONNX/tokenizer equivalence to the named Ollama revision remains an
  operator-provisioned assertion in v1; Sonder verifies identity, dimension,
  declared pipeline, and every supplied byte, but does not claim to derive the
  ONNX conversion from Ollama automatically.

## Provisioning the default bundles

Two operator scripts automate the bundle provisioning NPU.md requires; both
write only under the manifest directory and refuse to emit a manifest that
fails verification:

- `scripts/npu_distill_router.py` — builds `exec-route-v1`. Generates a
  deterministic corpus of ambiguous-band prompts, labels each with the real
  local router (`_execution_route_model` at temperature 0, cached to a JSONL
  sidecar), trains a small numpy MLP on `route_features` vectors, and exports
  a self-contained ONNX + pinned manifest. The bundle is a distillation of
  the existing local decision, not a new policy; `--min-agreement` (default
  0.8) gates manifest emission on holdout agreement with the baseline.
- `scripts/npu_provision_embedder.py` — builds `embed-npu-v1`. Downloads the
  operator-chosen ONNX export (default `nomic-ai/nomic-embed-text-v1.5`),
  verifies numerical equivalence against the live Ollama embedder (cosine on
  probe texts, `--min-cosine` default 0.999), pins every byte, and writes the
  manifest with `space` bound to the current local Ollama manifest revision.
  This turns the "operator-provisioned assertion" of ONNX/Ollama equivalence
  into a measured, reproducible check.

Model files live in `models/` under the manifest directory so the `*.json`
manifest glob never parses them.

Measured on the reference machine (Ryzen 7840HS, CPU provider, 2026-08):
worker cold start ~6 s (spawn + full-bundle SHA-256 + session load), warm
embedding ~23 ms (vs the Ollama HTTP path), warm routing ~1.4 ms (vs a 1–2 s
router-tier LLM call), embedder worker RSS ~1.1 GiB. Distilled routing
holdout agreement with the baseline router was 0.665 against a 0.626
majority rate on a synthetic in-band corpus with the v1 features, and 0.680
with the v2 semantic contract (`exec-route-features-v2`, 36 dims adding
verb/target-class and structure features). The residual gap is partly the
baseline itself: the router-tier LLM flipped 12.5% of labels under trivial
meaning-preserving paraphrase, capping any distillation near ~0.87. Routing
therefore ships in **shadow** (measuring live agreement, never changing
behavior) while embeddings can run **prefer**; promoting routing to prefer
should be an evidence call from live shadow agreement, or a decision to
distill from a stronger local judge than the production router tier.

Stronger-judge follow-up (2026-08-02, `npu_distill_router --judge-tier
code`): the code-tier judge is more stable (7.5% paraphrase flips vs the
router tier's 12.5%) and far more decisive — it routed 90.6% of the
synthetic ambiguous band to workbench. The judge-distilled bundle is
deployed in shadow, so live shadow disagreement now measures the *policy
difference* between the judge and the production baseline, not distillation
fidelity. Prefer remains withheld: the distilled holdout (0.869) sits below
the skewed labels' majority rate (0.906), and the autopilot minority has
too few training examples to trust its margins yet.

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

  AMD field notes (2026-08, Ryzen 7840HS / Phoenix XDNA1): AMD publishes
  `onnxruntime-vitisai` wheels on `https://pypi.amd.com/simple` (cp312,
  built against numpy 1.x), but the wheel alone cannot compile to the NPU —
  its VAIP/VAIML compiler (`vaiml.dll`, xclbin firmware) ships only in the
  Ryzen AI Software installer, which sits behind an AMD-account EULA
  download. The alternative Windows ML catalog distributes a signed VitisAI
  EP via Microsoft Store with no AMD account, but gates on an exact NPU
  driver window (32.00.0203.280–297 at the time of writing); machines on a
  newer NPU driver are filtered out of the catalog until AMD widens the
  range. Until one of those vendor steps is taken, the `cpu` reference
  provider serves the same pinned bundles and every manifest here lists
  `vitisai` first so the NPU engages on the next successful vendor-runtime
  load.
- `pip install tokenizers` — required for real (non-simulated) embedding
  bundles with an `hf-tokenizers` tokenizer.

Environment knobs (all optional): `SONDER_NPU_MANIFEST_DIR`,
`SONDER_NPU_MIN_FREE_RAM_GB` (default 2), `SONDER_NPU_MAX_RSS_MB` (default
1536), `SONDER_NPU_IDLE_UNLOAD_S` (default 300),
`SONDER_NPU_CIRCUIT_COOLDOWN_S` (default 120). `SONDER_NPU_TEST_HOOKS=1`
enables test-only worker hooks and is never set in production.
