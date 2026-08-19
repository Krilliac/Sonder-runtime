"""SPEC-5 WP12 — Release hardening acceptance tests.

These tests verify the end-state architecture is fully wired and
operational. Items that require live infrastructure (Ollama, NPU,
container isolation) are marked with appropriate skip conditions.
"""
from __future__ import annotations

import ast
import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "sonder_runtime"


# --- 1. Full behavioral suite ---

def test_all_spec5_domain_modules_importable():
    modules = [
        "sonder_runtime.domain.common.errors",
        "sonder_runtime.domain.common.ids",
        "sonder_runtime.domain.common.events",
        "sonder_runtime.domain.runtime_policy.rules",
        "sonder_runtime.domain.routing.route_planner",
        "sonder_runtime.domain.tools.descriptors",
        "sonder_runtime.domain.tools.policy",
        "sonder_runtime.domain.automation.models",
        "sonder_runtime.domain.selfmod.models",
        "sonder_runtime.domain.training.models",
        "sonder_runtime.domain.updates.models",
    ]
    for name in modules:
        mod = importlib.import_module(name)
        assert mod is not None, f"Failed to import {name}"


def test_all_spec5_application_services_importable():
    modules = [
        "sonder_runtime.application.context",
        "sonder_runtime.application.errors",
        "sonder_runtime.application.memory.recall_service",
        "sonder_runtime.application.memory.outcome_service",
        "sonder_runtime.application.execution.tool_service",
        "sonder_runtime.application.automation.automation_service",
        "sonder_runtime.application.selfmod.selfmod_service",
        "sonder_runtime.application.training.training_service",
        "sonder_runtime.application.updates.update_service",
    ]
    for name in modules:
        mod = importlib.import_module(name)
        assert mod is not None, f"Failed to import {name}"


def test_all_spec5_interface_modules_importable():
    modules = [
        "sonder_runtime.interfaces.http.handlers",
        "sonder_runtime.interfaces.mcp.handlers",
        "sonder_runtime.interfaces.cli.commands",
        "sonder_runtime.interfaces.repl.handler",
    ]
    for name in modules:
        mod = importlib.import_module(name)
        assert mod is not None, f"Failed to import {name}"


def test_bootstrap_container_builds():
    from sonder_runtime.bootstrap.container import (
        RuntimeCapabilities,
        RuntimeConfig,
        build_runtime,
    )

    config = RuntimeConfig(profile="workstation-local")
    caps = RuntimeCapabilities()
    runtime = build_runtime(config, caps)
    assert runtime is not None
    assert runtime.config.profile == "workstation-local"


# --- 2. Production acceptance: architecture holds end-to-end ---

def test_architecture_checker_passes():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_architecture.py")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_adapters_legacy_directory():
    legacy = PACKAGE_ROOT / "adapters" / "legacy"
    assert not legacy.exists(), "adapters/legacy/ must not exist in end-state"


# --- 3. Crash-injection matrix ---

def test_operation_context_deadline_enforcement():
    from sonder_runtime.application.context import local_owner_context

    ctx = local_owner_context(
        correlation_id="wp12-test",
        timeout_seconds=0.0,
    )
    assert ctx.expired


def test_domain_errors_are_picklable():
    import pickle
    from sonder_runtime.domain.common.errors import (
        InvalidInput,
        NotFound,
        Forbidden,
        DeadlineExceeded,
        Cancelled,
        DependencyUnavailable,
    )

    for cls in (InvalidInput, NotFound, Forbidden, DeadlineExceeded,
                Cancelled, DependencyUnavailable):
        err = cls("test message")
        roundtrip = pickle.loads(pickle.dumps(err))
        assert str(roundtrip) == "test message"
        assert type(roundtrip) is cls


# --- 4. Backup/restore ---

def test_backup_service_interface_contract():
    from sonder_runtime.application.backup import BackupService
    from sonder_runtime.application.ports.backup import BackupGateway

    import inspect
    sig = inspect.signature(BackupService.__init__)
    params = list(sig.parameters.keys())
    assert "gateway" in params


# --- 5. Signed update/rollback ---

def test_update_domain_phase_transitions():
    from sonder_runtime.domain.updates.models import UpdatePhase

    phases = [e.value for e in UpdatePhase]
    assert "available" in phases
    assert "verified" in phases
    assert "activated" in phases
    assert "rolled_back" in phases


def test_update_run_can_activate_only_when_verified():
    from sonder_runtime.domain.updates.models import (
        UpdatePhase,
        UpdateRun,
        ReleaseMetadata,
    )

    release = ReleaseMetadata(version="1.0.0", channel="stable")
    run = UpdateRun(
        id="u1",
        release=release,
        phase=UpdatePhase.AVAILABLE,
    )
    assert not run.can_activate

    draining = UpdateRun(
        id="u1",
        release=release,
        phase=UpdatePhase.DRAINING,
        backup_path="/tmp/backup",
        verified=True,
        health_ok=True,
    )
    assert draining.can_activate


# --- 6. Container isolation acceptance ---

@pytest.mark.skipif(
    os.environ.get("SONDER_CONTAINER_TEST") != "1",
    reason="requires SONDER_CONTAINER_TEST=1 and container runtime",
)
def test_container_isolation():
    pytest.skip("Container isolation requires live runtime")


# --- 7. Guarded/unrestricted capability matrix ---

def test_operation_context_auth_levels():
    from sonder_runtime.application.context import local_owner_context

    for level in ("user", "developer", "admin"):
        ctx = local_owner_context(
            correlation_id=f"wp12-{level}",
            auth_level=level,
        )
        assert ctx.auth_level == level


# --- 8. Selfmod recovery ---

def test_selfmod_lifecycle_states():
    from sonder_runtime.domain.selfmod.models import SelfmodPhase

    phases = [e.value for e in SelfmodPhase]
    assert "proposed" in phases
    assert "approved" in phases
    assert "deployed" in phases
    assert "restored" in phases


# --- 9. Training smoke ---

def test_training_lifecycle_states():
    from sonder_runtime.domain.training.models import TrainingPhase

    phases = [e.value for e in TrainingPhase]
    assert "planned" in phases
    assert "dataset_locked" in phases
    assert "training" in phases
    assert "candidate" in phases
    assert "deployed" in phases


def test_model_identity_immutable():
    from sonder_runtime.domain.training.models import ModelIdentity

    identity = ModelIdentity(
        run_id="r1", base_model="test", base_revision="abc123",
        dataset_digest="sha256:aaa",
    )
    with pytest.raises(AttributeError):
        identity.run_id = "changed"


# --- 10. MCP v2 client stubs ---

def test_mcp_handler_classes_exist():
    from sonder_runtime.interfaces.mcp.handlers import (
        McpRecallHandler,
        McpOutcomeHandler,
        McpToolHandler,
    )

    assert McpRecallHandler is not None
    assert McpOutcomeHandler is not None
    assert McpToolHandler is not None


# --- 11. Static architecture mutation tests ---

def _isolated_checker(tmp_path):
    """Stage a throwaway copy of the package and return its checker path.

    These tests prove the architecture checker is not vacuously green, which
    requires a real violating module on disk. It must not be THIS disk:
    `check()` walks the package with `rglob`, so a plant in the live tree is
    seen by anything else reading it -- a concurrent run, a developer's
    `check_architecture.py`, or `test_architecture_checker_passes` above -- and
    a crash before the `finally` leaves it there for good.

    `check_architecture.py` derives its package root from its own location, so
    a copy exercises the identical code path. The copy is `git init`-ed because
    the checker's production inventory comes from `git ls-files`.
    """
    shutil.copytree(PACKAGE_ROOT, tmp_path / "sonder_runtime")
    (tmp_path / "scripts").mkdir()
    checker = tmp_path / "scripts" / "check_architecture.py"
    shutil.copy2(REPO_ROOT / "scripts" / "check_architecture.py", checker)

    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        staged = subprocess.run(
            command, cwd=tmp_path, capture_output=True, text=True, timeout=120,
        )
        if staged.returncode != 0:
            pytest.skip(
                "git is required to stage the isolated copy: %s"
                % (staged.stderr.strip() or staged.stdout.strip())
            )

    clean = subprocess.run(
        [sys.executable, str(checker)],
        capture_output=True, text=True, timeout=120,
    )
    assert clean.returncode == 0, (
        "the copied tree must start clean, or a violation planted in it "
        "proves nothing:\n" + clean.stdout + clean.stderr
    )
    return checker


def _run_checker(checker):
    return subprocess.run(
        [sys.executable, str(checker)],
        capture_output=True, text=True, timeout=120,
    )


def test_domain_layer_rejects_adapter_import(tmp_path):
    checker = _isolated_checker(tmp_path)
    violation = (
        tmp_path / "sonder_runtime" / "domain" / "common" / "_wp12_violation.py"
    )
    violation.write_text(
        "from sonder_runtime.adapters.filesystem import atomic_json\n",
        encoding="utf-8",
    )
    result = _run_checker(checker)
    assert result.returncode == 1
    assert "domain may not import" in result.stdout


def test_interface_layer_rejects_direct_adapter_import(tmp_path):
    checker = _isolated_checker(tmp_path)
    violation = (
        tmp_path / "sonder_runtime" / "interfaces" / "http" / "_wp12_violation.py"
    )
    violation.write_text(
        "from sonder_runtime.adapters.strangler_services import SystemClock\n",
        encoding="utf-8",
    )
    result = _run_checker(checker)
    assert result.returncode == 1
    # Was `... in result.stdout or result.returncode == 1`. The right disjunct
    # is the line above, so the message check could never fail and the test
    # could not tell this violation from any other. Name the layer.
    assert "interfaces" in result.stdout and "may not import" in result.stdout, (
        result.stdout
    )


def test_application_layer_rejects_platform_import(tmp_path):
    checker = _isolated_checker(tmp_path)
    violation = (
        tmp_path / "sonder_runtime" / "application" / "memory"
        / "_wp12_violation.py"
    )
    violation.write_text(
        "import sonder_config\n",
        encoding="utf-8",
    )
    result = _run_checker(checker)
    assert result.returncode == 1
    assert "application" in result.stdout and "may not import" in result.stdout, (
        result.stdout
    )


# --- 12. Clean installation and bridge upgrade ---

def test_composition_root_builds_without_legacy_directory():
    assert not (PACKAGE_ROOT / "adapters" / "legacy").exists()
    from sonder_runtime.bootstrap.app import build_application

    app = build_application()
    assert app.profile == "workstation-local"


def test_spec5_container_builds_without_legacy():
    assert not (PACKAGE_ROOT / "adapters" / "legacy").exists()
    from sonder_runtime.bootstrap.container import (
        RuntimeCapabilities,
        RuntimeConfig,
        build_runtime,
    )

    runtime = build_runtime(
        RuntimeConfig(profile="workstation-local"),
        RuntimeCapabilities(),
    )
    assert runtime is not None


# --- Layer inventory completeness ---

def test_all_spec5_layers_have_init_files():
    layers = [
        PACKAGE_ROOT / "domain",
        PACKAGE_ROOT / "application",
        PACKAGE_ROOT / "adapters",
        PACKAGE_ROOT / "interfaces",
        PACKAGE_ROOT / "bootstrap",
    ]
    for layer in layers:
        assert layer.is_dir(), f"Missing layer: {layer}"
        assert (layer / "__init__.py").exists(), f"Missing __init__.py in {layer}"


def test_no_circular_imports_in_domain():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sonder_runtime.domain.common.errors; "
         "import sonder_runtime.domain.common.ids; "
         "import sonder_runtime.domain.common.events; "
         "import sonder_runtime.domain.routing.route_planner; "
         "import sonder_runtime.domain.tools.descriptors; "
         "import sonder_runtime.domain.tools.policy; "
         "import sonder_runtime.domain.automation.models; "
         "import sonder_runtime.domain.selfmod.models; "
         "import sonder_runtime.domain.training.models; "
         "import sonder_runtime.domain.updates.models; "
         "print('OK')"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
