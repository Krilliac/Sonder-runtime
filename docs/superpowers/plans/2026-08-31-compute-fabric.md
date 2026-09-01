# Compute Fabric Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure, resource-aware compute fabric that inventories configured private nodes and places bounded whole-job workloads without duplicating Sonder's inference router or permitting arbitrary remote shell access.

**Architecture:** Pure domain contracts validate nodes, snapshots, workload requirements, and deterministic placement. Typed configuration and adapters supply static authority plus independently measured local/remote telemetry; a catalog-bound remote-job facade reuses Sonder's authenticated HTTP and durable-job boundaries. Inference remains owned by the existing model gateway and Ollama pool.

**Tech Stack:** Python 3.11+ dataclasses/enums/protocols, stdlib `urllib`, existing Sonder typed configuration, HTTP handler, process-job registry, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-compute-fabric-design.md`

## Global Constraints

- Existing installations with no compute configuration must behave unchanged.
- At most 15 remote compute nodes may be configured in the first slice.
- Every non-local origin must be HTTPS, contain no inline credentials, and pass the explicit remote-consent gate.
- Static configuration establishes maximum authority; telemetry may only narrow it.
- Unknown measurements never count as resource headroom or readiness.
- The scheduler places whole jobs; it does not shard models or provide remote memory.
- Remote execution uses worker-owned catalog entry ids, never arbitrary command strings or controller absolute paths.
- Request/response bodies, output previews, argv, environment, timeouts, and artifact lists must be bounded.
- Inference dispatch remains owned by the existing model gateway and Ollama pool.
- No new third-party runtime dependency is allowed.

---

## File structure

- `sonder_runtime/domain/compute_fabric.py`: immutable vocabulary, validation, digests, and pure placement scheduler.
- `sonder_runtime/application/ports/compute_fabric.py`: registry, snapshot-source, and remote-job transport protocols.
- `sonder_runtime/application/compute_fabric/registry.py`: configured-node authority plus TTL-observation reconciliation.
- `sonder_runtime/application/compute_fabric/jobs.py`: catalog definitions, envelopes, receipts, and controller/worker orchestration.
- `sonder_runtime/adapters/compute_fabric/local_snapshot.py`: bounded local hardware/tool/job observation.
- `sonder_runtime/adapters/compute_fabric/http_client.py`: strict HTTPS snapshot/job client.
- `sonder_runtime/interfaces/http/facades/compute_fabric.py`: authenticated bounded worker API presentation.
- `sonder_runtime/platform/config.py`: typed compute/node/catalog configuration and validation.
- `sonder_runtime/bootstrap/app.py`: lazy composition and control-plane projection.
- `sonder_runtime/interfaces/http/serve.py`: four explicit compute routes delegated to the facade.
- `docs/runbooks/compute-fabric.md`: deployment, security, configuration, and recovery procedure.

### Task 1: Pure node, workload, and placement domain

**Files:**
- Create: `sonder_runtime/domain/compute_fabric.py`
- Test: `tests/test_compute_fabric_domain.py`

**Interfaces:**
- Produces: `WorkloadKind`, `ComputeCapability`, `NodeHealth`, `NodeResources`, `ComputeNode`, `NodeSnapshot`, `WorkloadRequest`, `CandidateDecision`, `PlacementDecision`, `ComputePlacementScheduler.place(request, snapshots, now)`.
- Consumes: only stdlib values and `datetime`.

- [ ] **Step 1: Write failing validation and placement tests**

```python
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest

from sonder_runtime.domain.compute_fabric import (
    ComputeCapability, ComputeNode, ComputePlacementScheduler, NodeHealth,
    NodeResources, NodeSnapshot, WorkloadKind, WorkloadRequest,
)

NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)

def snapshot(node_id, *, local=False, free_ram=16 << 30, load=0.2, jobs=0,
             capabilities=(ComputeCapability.CPU, ComputeCapability.CMAKE)):
    node = ComputeNode(
        node_id=node_id,
        origin=None if local else f"https://{node_id}.example:8443",
        local=local,
        allowed_workloads=frozenset({WorkloadKind.BUILD, WorkloadKind.TEST}),
        configured_capabilities=frozenset(capabilities),
        workspace_mappings=frozenset({"sonder"}),
    )
    return NodeSnapshot(
        node=node, observed_at=NOW, health=NodeHealth.HEALTHY,
        live_capabilities=frozenset(capabilities),
        resources=NodeResources(free_ram_bytes=free_ram, load_fraction=load),
        active_jobs=jobs, round_trip_ms=5.0,
    )

def test_remote_nodes_require_https_without_credentials():
    with pytest.raises(ValueError, match="HTTPS"):
        ComputeNode("bad", "http://worker/run", False, frozenset({WorkloadKind.BUILD}))
    with pytest.raises(ValueError, match="credentials"):
        ComputeNode("bad", "https://u:p@worker/run", False, frozenset({WorkloadKind.BUILD}))

def test_scheduler_rejects_stale_or_under_resourced_nodes_and_is_deterministic():
    request = WorkloadRequest(
        request_id="r1", kind=WorkloadKind.BUILD,
        required_capabilities=frozenset({ComputeCapability.CMAKE}),
        workspace_mapping="sonder", min_free_ram_bytes=8 << 30,
    )
    stale = snapshot("stale")
    stale = replace(stale, observed_at=NOW - timedelta(minutes=2))
    low = snapshot("low", free_ram=4 << 30)
    best = snapshot("node-b", load=0.1)
    tie = snapshot("node-a", load=0.1)
    result = ComputePlacementScheduler(snapshot_ttl=timedelta(seconds=30)).place(
        request, (stale, low, best, tie), now=NOW,
    )
    assert result.selected_node_id == "node-a"
    assert {item.node_id: item.reason_code for item in result.candidates} == {
        "low": "insufficient_ram", "node-a": "eligible",
        "node-b": "eligible", "stale": "stale",
    }

def test_private_work_never_uses_remote_node_and_inference_is_projection_only():
    private = WorkloadRequest("r", WorkloadKind.TEST, local_only=True)
    assert ComputePlacementScheduler().place(private, (snapshot("remote"),), now=NOW).selected_node_id is None
    inference = WorkloadRequest("i", WorkloadKind.INFERENCE)
    with pytest.raises(ValueError, match="model gateway"):
        ComputePlacementScheduler().place(inference, (snapshot("remote"),), now=NOW)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_fabric_domain.py -q`

Expected: collection fails because `sonder_runtime.domain.compute_fabric` does not exist.

- [ ] **Step 3: Implement the immutable domain and pure scheduler**

Implement enum-backed bounded vocabularies, strict `__post_init__` validation,
canonical JSON SHA-256 projection, hard-constraint reason codes, and deterministic
score ordering. `NodeSnapshot.effective_capabilities` must intersect configured
and live capabilities. `PlacementDecision.selected_node_id` is `None` when no
candidate is eligible. Resource score uses only known values and never divides
by zero or accepts non-finite floats.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_fabric_domain.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the domain slice**

```powershell
git add -- sonder_runtime/domain/compute_fabric.py tests/test_compute_fabric_domain.py
git commit -s -m "feat: add compute fabric placement domain"
```

### Task 2: Typed compute configuration and static registry

**Files:**
- Modify: `sonder_runtime/platform/config.py`
- Create: `sonder_runtime/application/ports/compute_fabric.py`
- Create: `sonder_runtime/application/compute_fabric/__init__.py`
- Create: `sonder_runtime/application/compute_fabric/registry.py`
- Test: `tests/test_compute_fabric_config.py`
- Test: `tests/test_compute_node_registry.py`

**Interfaces:**
- Consumes: Task 1 `ComputeNode`, `NodeSnapshot`, `WorkloadKind`, `ComputeCapability`.
- Produces: `ComputeConfig`, `ComputeNodeConfig`, `ComputeJobConfig`, `ComputeSnapshotSource.snapshot(node)`, and `ComputeNodeRegistry.observe/list_snapshots/get_node`.

- [ ] **Step 1: Write failing config and authority tests**

```python
def test_default_compute_config_is_local_only_and_remote_disabled():
    config = load_config(environ={})
    assert config.compute.allow_remote is False
    assert config.compute.nodes == ()
    assert config.compute.snapshot_ttl_seconds == 30

def test_remote_node_requires_consent_https_bounded_vocabulary():
    raw = base_mapping(compute={
        "allow_remote": True,
        "nodes": [{"id": "n1", "origin": "http://n1:8443", "workloads": ["build"]}],
    })
    with pytest.raises(ConfigError, match="HTTPS"):
        config_from_mapping(raw)

def test_registry_telemetry_cannot_widen_static_authority():
    configured = node(allowed={WorkloadKind.BUILD}, capabilities={ComputeCapability.CMAKE})
    registry = ComputeNodeRegistry((configured,), snapshot_ttl=timedelta(seconds=30))
    advertised = snapshot(configured, workloads={WorkloadKind.BUILD, WorkloadKind.SERVICE},
                          capabilities={ComputeCapability.CMAKE, ComputeCapability.DOCKER})
    registry.observe(advertised)
    effective = registry.list_snapshots(now=NOW)[0]
    assert effective.allowed_workloads == frozenset({WorkloadKind.BUILD})
    assert effective.effective_capabilities == frozenset({ComputeCapability.CMAKE})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_fabric_config.py tests/test_compute_node_registry.py -q`

Expected: missing compute configuration and registry types.

- [ ] **Step 3: Implement configuration parsing and registry reconciliation**

Add frozen config dataclasses, mapping/TOML/env parsing, validation for at most
15 remote nodes, unique ids, origin security, bounded lists, positive limits,
unique catalog ids, and workload/capability vocabulary. Add `compute` to
`SonderConfig` with a backwards-compatible default. The registry must reject an
observation for an unknown or mismatched node id and return deterministic order.

- [ ] **Step 4: Run focused and existing configuration tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_fabric_config.py tests/test_compute_node_registry.py tests/test_config_* -q`

Expected: all pass and legacy config fixtures need no compute keys.

- [ ] **Step 5: Commit the configuration slice**

```powershell
git add -- sonder_runtime/platform/config.py sonder_runtime/application/ports/compute_fabric.py sonder_runtime/application/compute_fabric/__init__.py sonder_runtime/application/compute_fabric/registry.py tests/test_compute_fabric_config.py tests/test_compute_node_registry.py
git commit -s -m "feat: configure compute node registry"
```

### Task 3: Local snapshots and read-only control-plane status

**Files:**
- Create: `sonder_runtime/adapters/compute_fabric/__init__.py`
- Create: `sonder_runtime/adapters/compute_fabric/local_snapshot.py`
- Modify: `sonder_runtime/bootstrap/app.py`
- Modify: `sonder_runtime/application/control_plane/snapshot.py`
- Test: `tests/test_compute_local_snapshot.py`
- Test: `tests/test_compute_fabric_composition.py`
- Modify: `tests/test_control_plane_snapshot.py`

**Interfaces:**
- Consumes: registry/snapshot types from Tasks 1-2; existing hardware, GPU, environment, storage, and job-count probes.
- Produces: `LocalComputeSnapshotSource.snapshot(node, now)` and lazy `Application.compute_registry/compute_scheduler/compute_snapshot` accessors.

- [ ] **Step 1: Write failing conservative-probe tests**

```python
def test_local_snapshot_reports_measured_values_without_inventing_readiness(monkeypatch):
    probes = FakeProbes(cpu_count=16, total_ram=32 << 30, free_ram=12 << 30,
                        disk_free=400 << 30, load_fraction=None,
                        tools={"cmake": "C:/cmake.exe"}, gpu_vendor="NVIDIA", cuda_ready=False)
    result = LocalComputeSnapshotSource(probes, active_jobs=lambda: 2).snapshot(local_node(), now=NOW)
    assert result.resources.free_ram_bytes == 12 << 30
    assert result.active_jobs == 2
    assert ComputeCapability.CMAKE in result.live_capabilities
    assert ComputeCapability.CUDA not in result.live_capabilities

def test_application_composes_local_fabric_lazily_without_network(monkeypatch):
    monkeypatch.setattr(http_client, "urlopen", lambda *_a, **_k: pytest.fail("network"))
    app = build_application(config=SonderConfig())
    assert app.compute_registry().get_node("local").local
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_local_snapshot.py tests/test_compute_fabric_composition.py -q`

Expected: missing local snapshot and composition properties.

- [ ] **Step 3: Implement conservative local collection and lazy composition**

Adapt existing probes rather than starting unbounded new processes. Tool
presence becomes only the matching static capability; CUDA requires existing
CUDA-ready evidence. Add `compute_fabric` as a control-plane section containing
bounded node summaries and placement counters. Update the snapshot section
vocabulary and its exact-name tests.

- [ ] **Step 4: Run focused tests and architecture boundary tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_local_snapshot.py tests/test_compute_fabric_composition.py tests/test_control_plane_snapshot.py tests/test_bootstrap_app.py -q`

Expected: all pass.

- [ ] **Step 5: Commit the local/status slice**

```powershell
git add -- sonder_runtime/adapters/compute_fabric sonder_runtime/bootstrap/app.py sonder_runtime/application/control_plane/snapshot.py tests/test_compute_local_snapshot.py tests/test_compute_fabric_composition.py tests/test_control_plane_snapshot.py
git commit -s -m "feat: expose local compute fabric status"
```

### Task 4: Strict remote snapshot transport

**Files:**
- Create: `sonder_runtime/adapters/compute_fabric/http_client.py`
- Create: `sonder_runtime/interfaces/http/facades/compute_fabric.py`
- Modify: `sonder_runtime/interfaces/http/serve.py`
- Test: `tests/test_compute_snapshot_http_client.py`
- Test: `tests/test_compute_snapshot_http.py`

**Interfaces:**
- Consumes: `ComputeSnapshotSource`, Task 1 serialization and digest validation, Task 3 local source.
- Produces: `HttpsComputeSnapshotSource.snapshot(node, now)` and authenticated `GET /v1/compute/snapshot`.

- [ ] **Step 1: Write failing endpoint/client security tests**

```python
def test_snapshot_endpoint_requires_authenticated_reader(http_server):
    status, body = get_json(http_server, "/v1/compute/snapshot")
    assert status == 401

def test_snapshot_client_rejects_redirect_identity_mismatch_and_oversize(monkeypatch):
    node = remote_node("configured", "https://node.example:8443")
    client = HttpsComputeSnapshotSource(api_key="secret", max_response_bytes=4096)
    monkeypatch.setattr(client, "_open", lambda *_: response(302, b"", {"Location": "https://other"}))
    with pytest.raises(DependencyUnavailable, match="redirect"):
        client.snapshot(node, now=NOW)
    monkeypatch.setattr(client, "_open", lambda *_: response(200, snapshot_json("different")))
    with pytest.raises(DependencyUnavailable, match="identity"):
        client.snapshot(node, now=NOW)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_snapshot_http_client.py tests/test_compute_snapshot_http.py -q`

Expected: missing client/facade/routes.

- [ ] **Step 3: Implement bounded authenticated snapshot transport**

Use a no-redirect opener, explicit connect/read timeout, response byte cap,
JSON object/type/field/count bounds, exact node identity, timestamp freshness,
known vocabularies, and existing domain error translation. The server route
delegates to a facade and reuses `_request_auth_context`; it performs no probe
when auth fails.

- [ ] **Step 4: Run transport and existing HTTP auth tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_snapshot_http_client.py tests/test_compute_snapshot_http.py tests/test_http_auth.py tests/test_http_security.py -q`

Expected: all pass.

- [ ] **Step 5: Commit the snapshot transport**

```powershell
git add -- sonder_runtime/adapters/compute_fabric/http_client.py sonder_runtime/interfaces/http/facades/compute_fabric.py sonder_runtime/interfaces/http/serve.py tests/test_compute_snapshot_http_client.py tests/test_compute_snapshot_http.py
git commit -s -m "feat: add authenticated compute snapshots"
```

### Task 5: Catalog-bound remote job contracts

**Files:**
- Create: `sonder_runtime/application/compute_fabric/jobs.py`
- Modify: `sonder_runtime/application/ports/compute_fabric.py`
- Test: `tests/test_compute_job_contract.py`
- Test: `tests/test_compute_job_worker.py`

**Interfaces:**
- Consumes: existing `ProcessJobRequest`, `ProcessJobProvider`, `JobIdentity`; Task 2 `ComputeJobConfig`.
- Produces: `JobCatalogEntry`, `RemoteJobEnvelope`, `RemoteArtifactReceipt`, `RemoteJobReceipt`, `ComputeRemoteJobTransport`, `ComputeJobWorker.submit/status/cancel`.

- [ ] **Step 1: Write failing catalog, path, digest, and idempotency tests**

```python
def test_worker_resolves_catalog_program_and_never_accepts_controller_program(tmp_path):
    entry = JobCatalogEntry("pytest", WorkloadKind.TEST, program="python",
                            fixed_args=("-m", "pytest"), workspace_mappings=frozenset({"sonder"}))
    envelope = RemoteJobEnvelope.create(
        controller_job_id="c1", idempotency_key="i1", workload=WorkloadKind.TEST,
        catalog_entry_id="pytest", workspace_mapping="sonder", relative_cwd="tests",
        arguments=("test_api.py",), environment=(), deadline_seconds=60,
    )
    provider = CapturingProcessProvider()
    receipt = ComputeJobWorker({"pytest": entry}, {"sonder": tmp_path}, provider).submit(envelope)
    assert provider.request.argv == ("python", "-m", "pytest", "test_api.py")
    assert provider.request.cwd == tmp_path / "tests"
    assert receipt.request_sha256 == envelope.request_sha256

def test_worker_rejects_traversal_unknown_environment_and_digest_mismatch(tmp_path):
    worker = configured_worker(tmp_path)
    for envelope in (
        make_envelope(relative_cwd="../outside"),
        make_envelope(environment=(("SECRET", "x"),)),
        replace(make_envelope(), request_sha256="0" * 64),
    ):
        with pytest.raises(InvalidInput):
            worker.submit(envelope)

def test_same_idempotency_and_digest_returns_same_job_but_conflict_rejects():
    worker = configured_worker()
    first = worker.submit(make_envelope())
    assert worker.submit(make_envelope()).remote_job_id == first.remote_job_id
    with pytest.raises(Conflict):
        worker.submit(make_envelope(arguments=("different",)))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_job_contract.py tests/test_compute_job_worker.py -q`

Expected: missing job contracts/worker.

- [ ] **Step 3: Implement catalog-bound envelopes and worker orchestration**

Bound ids to 128 characters, args to 64 values/4096 bytes each, environment to
32 allow-listed keys, deadline to 1..86400 seconds, output previews to 64 KiB,
and artifacts to 256 receipts. Resolve relative paths with the existing
race-resistant path policy. Map to one durable process job; cache idempotency
by key plus request digest; return cleanup truth on cancel.

- [ ] **Step 4: Run job contract and durable-job tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_job_contract.py tests/test_compute_job_worker.py tests/test_generic_job_composition.py tests/test_job004_process_provider.py -q`

Expected: all pass.

- [ ] **Step 5: Commit the remote-job domain**

```powershell
git add -- sonder_runtime/application/compute_fabric/jobs.py sonder_runtime/application/ports/compute_fabric.py tests/test_compute_job_contract.py tests/test_compute_job_worker.py
git commit -s -m "feat: add catalog-bound compute jobs"
```

### Task 6: Remote job HTTP facade, client, and placement service

**Files:**
- Modify: `sonder_runtime/adapters/compute_fabric/http_client.py`
- Modify: `sonder_runtime/interfaces/http/facades/compute_fabric.py`
- Modify: `sonder_runtime/interfaces/http/serve.py`
- Modify: `sonder_runtime/bootstrap/app.py`
- Create: `sonder_runtime/application/compute_fabric/service.py`
- Test: `tests/test_compute_job_http.py`
- Test: `tests/test_compute_job_http_client.py`
- Test: `tests/test_compute_placement_service.py`

**Interfaces:**
- Consumes: Task 1 scheduler, Task 2 registry, Task 5 worker/transport.
- Produces: authenticated job routes, `HttpsComputeJobTransport`, and `ComputeFabricService.submit/status/cancel`.

- [ ] **Step 1: Write failing authorization, ambiguity, and fallback tests**

```python
def test_job_submit_requires_admin_and_rejects_unknown_fields(http_server):
    status, _ = post_json(http_server, "/v1/compute/jobs", valid_envelope(), auth=user_auth())
    assert status == 403
    status, body = post_json(http_server, "/v1/compute/jobs", {**valid_envelope(), "program": "cmd"}, auth=admin_auth())
    assert status == 400
    assert body["error"]["type"] == "invalid_request"

def test_submit_timeout_reconciles_idempotent_request_before_retry():
    transport = TimeoutThenExistingTransport()
    receipt = fabric_service(transport=transport).submit(build_request())
    assert receipt.remote_job_id == "already-running"
    assert transport.submit_calls == 1
    assert transport.lookup_calls == 1

def test_no_eligible_remote_node_uses_local_only_when_explicitly_allowed():
    service = fabric_service(remote_stale=True, local_healthy=True)
    assert service.submit(build_request(allow_local_fallback=True)).node_id == "local"
    with pytest.raises(DependencyUnavailable):
        service.submit(build_request(allow_local_fallback=False))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_job_http.py tests/test_compute_job_http_client.py tests/test_compute_placement_service.py -q`

Expected: missing routes/client/service.

- [ ] **Step 3: Implement worker API and controller service**

Add `POST /v1/compute/jobs`, `GET /v1/compute/jobs/{id}`, and
`POST /v1/compute/jobs/{id}/cancel` with strict exact routes and existing auth.
The client uses the same no-redirect/bounds rules as snapshots. The service
records a placement receipt before submit, queries by idempotency key after an
ambiguous failure, and never retries a non-idempotent request.

- [ ] **Step 4: Run compute and existing HTTP/job integration tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_job_http.py tests/test_compute_job_http_client.py tests/test_compute_placement_service.py tests/test_http_job_start.py tests/test_api003_restart_recovery.py -q`

Expected: all pass.

- [ ] **Step 5: Commit the end-to-end service**

```powershell
git add -- sonder_runtime/adapters/compute_fabric/http_client.py sonder_runtime/interfaces/http/facades/compute_fabric.py sonder_runtime/interfaces/http/serve.py sonder_runtime/bootstrap/app.py sonder_runtime/application/compute_fabric/service.py tests/test_compute_job_http.py tests/test_compute_job_http_client.py tests/test_compute_placement_service.py
git commit -s -m "feat: dispatch bounded compute jobs"
```

### Task 7: Workload profiles, runbook, and complete verification

**Files:**
- Create: `sonder_runtime/domain/compute_profiles.py`
- Create: `docs/runbooks/compute-fabric.md`
- Modify: `docs/wiki/07-agent-autopilot-fleet.md`
- Modify: `docs/wiki/08-model-tiers-and-gateway.md`
- Test: `tests/test_compute_profiles.py`

**Interfaces:**
- Consumes: Task 1 vocabularies/request type and Task 6 service.
- Produces: `profile_for(kind) -> WorkloadProfile` for build/test/index/analysis/fuzz/embedding/training/render/encode/service/container/storage.

- [ ] **Step 1: Write failing complete-profile coverage test**

```python
NON_INFERENCE = set(WorkloadKind) - {WorkloadKind.INFERENCE}

def test_every_non_inference_workload_has_a_bounded_profile():
    assert {profile_for(kind).kind for kind in NON_INFERENCE} == NON_INFERENCE
    assert profile_for(WorkloadKind.FUZZ).requires_deadline
    assert ComputeCapability.FFMPEG in profile_for(WorkloadKind.ENCODE).any_capabilities
    assert ComputeCapability.BLENDER in profile_for(WorkloadKind.RENDER).all_capabilities
    assert profile_for(WorkloadKind.SERVICE).requires_catalog_entry
    assert profile_for(WorkloadKind.CONTAINER).requires_digest_bound_input
```

- [ ] **Step 2: Run profile tests and verify RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_compute_profiles.py -q`

Expected: `compute_profiles` does not exist.

- [ ] **Step 3: Implement profiles and operational documentation**

Define frozen data-only profiles and make the service merge profile constraints
into requests before placement. Document direct-link/static networking,
TLS-proxy/auth requirements, local workspace mapping on each OS, catalog
examples for CMake/pytest/clang-tidy/ffmpeg/Blender/QLoRA, snapshot diagnosis,
stale-node recovery, ambiguous-submit reconciliation, cancellation truth, and
the explicit non-goals from the spec. State clearly which external tools must
be installed by the operator.

- [ ] **Step 4: Run focused, inference-regression, security, and full suites**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_compute_*.py tests/test_ollama_pool.py tests/test_provider_composition.py tests/test_remaining_execution_world_defaults.py -q
.venv\Scripts\python.exe -m pytest tests/test_http_security.py tests/test_http_auth.py tests/test_permission_gate_coverage.py -q
.venv\Scripts\python.exe -m pytest -q
```

Expected: focused and full suites pass; any environmental skips are reported,
not converted into success claims.

- [ ] **Step 5: Run static and packaging checks**

Run:

```powershell
.venv\Scripts\python.exe -m compileall -q sonder_runtime
.venv\Scripts\python.exe -m ruff check sonder_runtime/domain/compute_fabric.py sonder_runtime/domain/compute_profiles.py sonder_runtime/application/compute_fabric sonder_runtime/adapters/compute_fabric sonder_runtime/interfaces/http/facades/compute_fabric.py tests/test_compute_*.py
git diff --check
```

Expected: exit 0 for every command.

- [ ] **Step 6: Commit docs/profiles and integrate**

```powershell
git add -- sonder_runtime/domain/compute_profiles.py docs/runbooks/compute-fabric.md docs/wiki/07-agent-autopilot-fleet.md docs/wiki/08-model-tiers-and-gateway.md tests/test_compute_profiles.py docs/superpowers/specs/2026-08-31-compute-fabric-design.md docs/superpowers/plans/2026-08-31-compute-fabric.md
git commit -s -m "docs: operate the Sonder compute fabric"
git status --short
```

Then perform the repository's normal reviewed integration to `main`, confirm
the remote commit, and close any now-obsolete feature branch or PR without
rewriting history.
