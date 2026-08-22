# WP1 Sixteenth Slice: Package the NPU Process Boundary

Status: implemented on `agent/wp1-execution-status`.

## Scope

The NPU broker and worker process boundary now live under
`sonder_runtime.adapters.accelerators.npu`. Root-level `npu_broker.py` and
`npu_worker.py` are retired. The broker launches the packaged worker with
`python -m`, preserving repository import resolution for the child process.

The architecture checker has an explicit, path-scoped policy for the NPU
accelerator's optional vendor dependencies (`numpy`, `onnxruntime`, and
`tokenizers`) and the existing `system_profile` platform boundary. This keeps
the exception narrow to the accelerator process boundary rather than allowing
third-party imports throughout the adapter layer.

## Evidence

- NPU contract, manifest, provider, broker, service, embedding, package-local
  system, and architecture tests: **228 passed, 5 skipped**.
- Direct packaged-worker smoke test emitted the `ready` event and answered a
  `hello` request over the JSONL protocol.
- `scripts/check_architecture.py`: expected to be rerun with the staged slice.
- `scripts/check_requirement_evidence.py`: expected to be rerun with the
  staged slice.

## Remaining boundary

The NPU service has since joined the same package boundary. The next
observability boundary is the response activity tracker, now covered by the
seventeenth slice. The immutable baseline migration's `memory_store.py`
compatibility exception remains documented separately.
