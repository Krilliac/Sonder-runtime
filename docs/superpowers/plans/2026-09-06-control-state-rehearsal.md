# Control-state rehearsal implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide an explicit, fail-closed command that gathers external control-state evidence for a disposable pooled-pair rehearsal without promoting an owner or changing the normal runtime.

**Architecture:** A typed configuration section and secrets-only key feed a small bootstrap factory that constructs the existing HTTPS provider and coordinator only for the new command. The command emits bounded redacted evidence; a process-based test fixture exercises the real loopback transport and proves both success and refusal paths.

**Tech Stack:** Python 3.11 standard library, existing Sonder configuration, HTTP control-state adapter, pytest, Windows-safe multiprocessing.

**Spec:** `docs/superpowers/specs/2026-09-06-control-state-rehearsal-design.md`

## Global Constraints

- The configuration defaults to disabled and normal `serve`, `mcp`, and `repl` composition must never instantiate the provider.
- Secrets enter only through the secrets environment; TOML, diagnostics, logs, and reports must not expose them.
- Remote origins require HTTPS. HTTP is accepted only for an explicit loopback-only test configuration.
- The command must never promote an owner, mutate a local ownership lease, or state that automatic failover/failback is available.
- Tests use disposable paths and bounded child-process cleanup; they do not contact a real remote node or modify the installed runtime.

---

### Task 1: Add disabled, typed rehearsal configuration

**Files:**
- Create: `sonder_runtime/platform/control_state_rehearsal_config.py`
- Modify: `sonder_runtime/platform/config.py`
- Test: `tests/test_control_state_rehearsal_config.py`

**Interfaces:**
- Produces `ControlStateRehearsalConfig` with `enabled`, `cluster_id`, `node_id`, `witness_id`, `provider_id`, `origin`, `timeout_seconds`, and `allow_insecure_loopback`.
- Produces `control_state_rehearsal_errors(config)` and the redacted `Secrets.control_state_rehearsal_key` field.
- Consumes existing `DeploymentConfig`, `ComputeConfig`, and the secrets environment parser.

- [ ] **Step 1: Write the failing configuration tests**

```python
def test_rehearsal_is_disabled_by_default():
    config = load_config(env={})
    assert config.control_state_rehearsal.enabled is False


def test_enabled_rehearsal_rejects_plain_remote_origin_and_missing_secret():
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path, env={})
    assert "remote control-state rehearsal origin must use HTTPS" in exc_info.value.errors
    assert "SONDER_CONTROL_STATE_REHEARSAL_API_KEY is required" in exc_info.value.errors
```

- [ ] **Step 2: Run the new test and verify the expected missing-interface failure**

Run: `python -m pytest -q tests/test_control_state_rehearsal_config.py`

Expected: FAIL because the configuration section and secret field do not exist.

- [ ] **Step 3: Implement the smallest typed configuration boundary**

```python
@dataclass(frozen=True)
class ControlStateRehearsalConfig:
    enabled: bool = False
    cluster_id: str = ""
    node_id: str = ""
    witness_id: str = ""
    provider_id: str = ""
    origin: str = ""
    timeout_seconds: int = 5
    allow_insecure_loopback: bool = False
```

Register the section in `SonderConfig` and `_SECTION_TYPES`; load the key only from the secret environment, redact it, and collect all configuration errors through the existing validation pipeline.

- [ ] **Step 4: Run focused configuration checks**

Run: `python -m pytest -q tests/test_control_state_rehearsal_config.py tests/test_config.py`

Expected: PASS, with valid loopback configuration accepted only when its explicit test-only switch and secret are supplied.

- [ ] **Step 5: Commit the configuration slice**

```bash
git add sonder_runtime/platform/control_state_rehearsal_config.py sonder_runtime/platform/config.py tests/test_control_state_rehearsal_config.py
git commit -s -m "feat(cluster): add explicit control-state rehearsal config"
```

### Task 2: Compose the existing provider only for rehearsal

**Files:**
- Create: `sonder_runtime/bootstrap/control_state_rehearsal.py`
- Test: `tests/test_control_state_rehearsal_bootstrap.py`

**Interfaces:**
- Produces `build_control_state_rehearsal(config) -> ExternalControlStateCoordinator`.
- Consumes `SonderConfig.control_state_rehearsal`, its secrets-only key, and two data identities from the validated pooled-pair deployment.
- Does not modify `bootstrap/app.py` or construct a provider during ordinary application startup.
- Revalidates the canonical rehearsal and deployment boundary before construction so a directly instantiated `SonderConfig` cannot bypass loader validation.

- [ ] **Step 1: Write failing bootstrap tests**

```python
def test_bootstrap_requires_enabled_pooled_pair_rehearsal():
    with pytest.raises(ValueError, match="control-state rehearsal is disabled"):
        build_control_state_rehearsal(default_config())


def test_bootstrap_builds_exact_two_data_replicas_and_distinct_witness():
    coordinator = build_control_state_rehearsal(enabled_pooled_pair_config())
    assert coordinator.capabilities.data_replica_ids == ("node-a", "node-b")
    assert coordinator.capabilities.witness_ids == ("witness-a",)
```

Add negative direct-construction coverage for disabled rehearsal, wrong profile,
missing or duplicate peer identity, local-node mismatch, witness overlap, invalid
boolean or timeout values, missing key, plaintext remote origin, and disabled
remote compute.  Each must fail before `HttpsControlStateProvider` is
constructed.  Add constructor-spy coverage for ordinary serve, MCP, and REPL
composition with both disabled and otherwise-valid enabled rehearsal
configuration; all ordinary paths must make zero factory/provider calls.

- [ ] **Step 2: Run the bootstrap test and verify the expected import failure**

Run: `python -m pytest -q tests/test_control_state_rehearsal_bootstrap.py`

Expected: FAIL because `build_control_state_rehearsal` does not exist.

- [ ] **Step 3: Implement the isolated bootstrap factory**

```python
def build_control_state_rehearsal(config: SonderConfig) -> ExternalControlStateCoordinator:
    """Construct a rehearsal-only provider; never construct an owner."""
```

Validate profile, enabled flag, two data identities, witness distinction, key presence, remote-compute permission, and adapter-accepted origin before building `HttpsControlStateProvider`. Reuse the canonical configuration/deployment validators and then enforce factory-only checks for direct callers; never stringify the configuration or secret in an error. Set `minimum_data_replicas=2`; never accept an implicit provider or a local SQLite fallback.

- [ ] **Step 4: Run bootstrap plus existing provider/coordinator regressions**

Run: `python -m pytest -q tests/test_control_state_rehearsal_bootstrap.py tests/test_http_control_state_provider.py tests/test_control_state_composition.py tests/test_cluster_availability.py`

Expected: PASS, with direct malformed configurations rejected before provider construction and ordinary serve, MCP, and REPL retaining zero rehearsal composition calls.

- [ ] **Step 5: Commit the bootstrap slice**

```bash
git add sonder_runtime/bootstrap/control_state_rehearsal.py tests/test_control_state_rehearsal_bootstrap.py
git commit -s -m "feat(cluster): compose rehearsal provider explicitly"
```

### Task 3: Expose a bounded, non-promoting command and process rehearsal

**Files:**
- Modify: `sonder_runtime/__main__.py`
- Modify: `docs/runbooks/control-state-provider.md`
- Create: `tests/test_control_state_rehearsal_command.py`
- Modify: `docs/architecture/generated/runtime-reference.json`
- Modify: `docs/architecture/generated/runtime-reference.md`

**Interfaces:**
- Produces `cmd_control_state_rehearsal(args) -> int` and a JSON-safe report containing `promotion_attempted: false`.
- Consumes `build_control_state_rehearsal`, explicit CLI resource/event fields, and `--confirm-fence external-fence` before any fencing request.
- The positive process test reads evidence only; it never asserts a role change.

- [ ] **Step 1: Write failing command and process-boundary tests**

```python
def test_command_reports_evidence_without_promotion(loopback_provider, capsys):
    code = main(["control-state-rehearsal", "--json", ...])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["promotion_attempted"] is False


def test_unavailable_provider_fails_closed_without_promotion(tmp_path):
    result = run_child_rehearsal(tmp_path, provider_available=False)
    assert result["exit_code"] != 0
    assert result["promotion_attempted"] is False
```

- [ ] **Step 2: Run tests and verify the expected parser/command failure**

Run: `python -m pytest -q tests/test_control_state_rehearsal_command.py`

Expected: FAIL because the command is not registered.

- [ ] **Step 3: Implement command with explicit fencing confirmation**

Build a strict `ControlStateEvent` from bounded arguments, append/read it, and only call `prepare_takeover` when `--confirm-fence external-fence` is present. Return JSON or text with no payload content or secret. Catch `DependencyUnavailable` and configuration errors as nonzero fail-closed outcomes. Never import or call a runtime-owner, deployment, promotion, or lease-mutation API.

- [ ] **Step 4: Add an out-of-process loopback test**

Use Windows-safe `multiprocessing.get_context("spawn")`, a loopback-only provider fixture, distinct child homes, readiness JSON, bounded joins, and `finally` cleanup. Assert distinct PIDs/state paths, exact provider request evidence, a successful non-promoting collection case, and an unavailable or malformed-provider denial with no extra provider action.

- [ ] **Step 5: Run focused and static verification**

Run: `python -m pytest -q tests/test_control_state_rehearsal_config.py tests/test_control_state_rehearsal_bootstrap.py tests/test_control_state_rehearsal_command.py tests/test_http_control_state_provider.py tests/test_control_state_composition.py tests/test_cluster_availability.py tests/test_takeover_readiness.py`

Run: `python -m compileall -q sonder_runtime`

Run: `python scripts/generate_runtime_reference.py --check`

Expected: PASS. The report and runbook must state that the loopback test is not independent-witness or automatic-HA evidence.

- [ ] **Step 6: Commit command, test, and docs slice**

```bash
git add sonder_runtime/__main__.py docs/runbooks/control-state-provider.md tests/test_control_state_rehearsal_command.py docs/architecture/generated/runtime-reference.json docs/architecture/generated/runtime-reference.md
git commit -s -m "feat(cluster): add bounded control-state rehearsal command"
```

### Task 4: Review, rebase, and publish evidence

**Files:**
- Modify: `outputs/sonder-current-candidate-evidence.json` (outside the repository)
- Modify: `outputs/sonder-overall-progress.md` (outside the repository)

**Interfaces:**
- Consumes the exact rebased PR head, protected-main SHA/tree, local focused output, and completed hosted check metadata.
- Produces evidence that distinguishes the process rehearsal from independent-witness or live-failover proof.

- [ ] **Step 1: Request independent security review**

Review the config/secret boundary, origin policy, command confirmation, report redaction, and test claim language. Record unavailable Daybreak capacity as unavailable review, not approval.

- [ ] **Step 2: Rebase and repeat focused gates**

Run the Task 3 suite, compileall, architecture, documentation authority/link, requirement evidence, and diff checks on the rebased head.

- [ ] **Step 3: Publish only after exact-head hosted checks pass**

```bash
git push --force-with-lease origin HEAD:codex/control-state-rehearsal
gh pr create --fill --base main
gh pr merge --auto --squash
```

- [ ] **Step 4: Record exact merged evidence and remaining limits**

State that the command has process-boundary evidence but not independent witness, automatic failover/failback, authoritative mobility, model sharding, or indefinite-scale proof.
