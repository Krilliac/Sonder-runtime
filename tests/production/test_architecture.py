"""SPEC-3 R-M12: the architecture checker holds and stays enforceable."""
from __future__ import annotations

import ast
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _architecture_module():
    import importlib.util

    path = _REPO_ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("architecture_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_architecture_check_passes():
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "check_architecture.py")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_root_allowlist_has_a_shrink_only_ratchet():
    import importlib.util

    path = _REPO_ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("architecture_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.ROOT_LEGACY_MODULES) <= module.ROOT_LEGACY_MODULE_LIMIT
    assert module.ROOT_LEGACY_MODULES == {"server"}
    assert "autopilot_store" not in module.ROOT_LEGACY_MODULES
    assert "memory_store" not in module.ROOT_LEGACY_MODULES
    assert Path("sonder_preflight.py") in module.RETIRED_ROOT_MODULES
    assert Path("model_transport.py") in module.RETIRED_ROOT_MODULES
    assert "model_transport" not in module.ROOT_LEGACY_MODULES
    assert "runtime_policy" not in module.ROOT_LEGACY_MODULES
    assert "eval_history" not in module.ROOT_LEGACY_MODULES
    assert "unsafe_lab" not in module.ROOT_LEGACY_MODULES
    assert module.ROOT_LEGACY_MODULES <= module.BASELINE_ROOT_LEGACY_MODULES

    removed = next(iter(module.ROOT_LEGACY_MODULES))
    module.ROOT_LEGACY_MODULES = (
        module.ROOT_LEGACY_MODULES - {removed}
    ) | {"new_accidental_legacy"}
    violations = module.check()
    assert any("new_accidental_legacy" in row for row in violations)


def test_unsafe_lab_stateful_owner_is_security_adapter(monkeypatch):
    """The compatibility module must preserve identity without app ownership."""
    import inspect
    import sys

    import unsafe_lab

    source = Path(inspect.getsourcefile(unsafe_lab)).resolve()
    assert source == (
        _REPO_ROOT / "sonder_runtime" / "adapters" / "security" / "unsafe_lab.py"
    ).resolve()
    assert not (
        _REPO_ROOT / "sonder_runtime" / "application" / "security" / "unsafe_lab.py"
    ).exists()
    assert sys.modules["unsafe_lab"] is unsafe_lab

    marker = []
    monkeypatch.setattr(unsafe_lab, "active", lambda: marker.append(True) or True)
    assert unsafe_lab.active() is True
    assert marker == [True]


def test_version_root_allowance_is_removed_after_packaged_ownership():
    module = _architecture_module()
    assert "sonder_version" not in module.ROOT_PLATFORM_MODULES

    offenders = []
    package_root = _REPO_ROOT / "sonder_runtime"
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "sonder_version" for alias in node.names
            ):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
                break
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "sonder_version"
            ):
                offenders.append(str(path.relative_to(_REPO_ROOT)))
                break
    assert offenders == []


def test_production_callers_use_the_memory_adapter():
    module = _architecture_module()
    assert module.compatibility_import_offenders(
        "memory_store",
        Path("memory_store.py"),
        allowed_paths=module.COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS["memory_store"],
    ) == ()


def test_autopilot_root_is_migration_only():
    module = _architecture_module()
    assert module.compatibility_import_offenders(
        "autopilot_store",
        Path("autopilot_store.py"),
        allowed_paths=module.COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS["autopilot_store"],
    ) == ()


def test_recall_root_module_is_retired_and_ratchet_shrank():
    import importlib.util

    checker = _REPO_ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("recall_architecture", checker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "recall" not in module.ROOT_LEGACY_MODULES
    assert module.ROOT_LEGACY_MODULE_LIMIT == 1

    assert Path("recall.py") in module.RETIRED_ROOT_MODULES
    assert "recall" not in module.COMPATIBILITY_ROOT_MODULES


def test_recall_use_case_depends_only_on_its_narrow_port():
    service = (
        _REPO_ROOT / "sonder_runtime" / "application" / "recall" / "use_cases.py"
    )
    tree = ast.parse(service.read_text(encoding="utf-8"), filename=str(service))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports == {"__future__", "ports.recall"}


# Directories `rglob` from the repo root walks that this ratchet does not
# govern. Without them the scan also reads the virtualenv (`.runtime/`), tool
# state (`.claude/`, `.superpowers/`), build output, and any git worktree
# checked out beneath the repo -- and a worktree is a second copy of this very
# repo, so every file in it is reported as an offender against itself.
_NOT_REPO_SOURCE = frozenset({
    "tests", "venv", ".venv", "node_modules", "build", "dist", "site-packages",
})


def _is_repo_source(relative: Path) -> bool:
    """Whether a repo-relative path is first-party source this ratchet covers."""
    return not any(
        part in _NOT_REPO_SOURCE or part.startswith(".")
        for part in relative.parts[:-1]
    )


def test_the_ratchet_scan_covers_source_and_excludes_vendored_trees():
    """The exclusion above must not be able to quietly exclude everything.

    An over-broad filter would make the ratchet scan nothing and pass forever,
    which is the same observable result as "no offenders". So assert both
    directions: real first-party modules are in, and the trees named above
    are out.
    """
    assert _is_repo_source(Path("server.py"))
    assert _is_repo_source(Path("sonder_runtime/__init__.py"))
    assert _is_repo_source(Path("scripts/check_architecture.py"))
    assert not _is_repo_source(Path("tests/test_x.py"))
    assert not _is_repo_source(Path(".runtime/Lib/site-packages/mcp/types.py"))
    assert not _is_repo_source(Path(".claude/worktrees/wt/server.py"))
    assert not _is_repo_source(Path("app/build/generated.py"))

    scanned = [
        path for path in _REPO_ROOT.rglob("*.py")
        if _is_repo_source(path.relative_to(_REPO_ROOT))
    ]
    assert len(scanned) > 100, "the exclusion filter swallowed the source tree"


def test_backup_root_module_is_retired_and_ratchet_shrank():
    import importlib.util

    checker = _REPO_ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("backup_architecture", checker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path("sonder_backup.py") in module.RETIRED_ROOT_MODULES
    assert module.ROOT_LEGACY_MODULE_LIMIT == 1

    offenders = []
    for path in _REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if relative == Path("sonder_backup.py") or not _is_repo_source(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "sonder_backup" for alias in node.names
            ):
                offenders.append(str(relative))
                break
            if (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == "sonder_backup"
            ):
                offenders.append(str(relative))
                break
    assert offenders == []


def test_storage_root_module_is_retired_and_ratchet_shrank():
    module = _architecture_module()
    assert Path("sonder_storage.py") in module.RETIRED_ROOT_MODULES


def test_backup_use_case_depends_only_on_its_narrow_port():
    service = (
        _REPO_ROOT / "sonder_runtime" / "application" / "backup" / "use_cases.py"
    )
    tree = ast.parse(service.read_text(encoding="utf-8"), filename=str(service))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports == {"__future__", "ports.backup"}


def test_applied_memory_baseline_remains_byte_for_byte_immutable():
    immutable_baseline = Path("migrations/memory/0001_baseline.py")
    module = _architecture_module()
    assert module.COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS == {
        "memory_store": frozenset({immutable_baseline}),
        "autopilot_store": frozenset({Path("migrations/autopilot/0001_baseline.py")}),
        "fleet_store": frozenset({Path("migrations/fleet/0001_baseline.py")}),
        "queued_actions": frozenset({Path("migrations/queued_actions/0001_baseline.py")}),
    }

    # Never rewrite an already-applied migration merely to satisfy the
    # strangler import rule. The root module is a true compatibility alias.
    baseline_path = _REPO_ROOT / immutable_baseline
    raw = baseline_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() in {
        # Git/Linux LF checkout and Windows CRLF checkout of the exact same
        # immutable migration source.
        "d13776e3ce5318049242f060fc4b6cbd17c4c8e5edf0dce920b12e9defeffb07",
        "ba186cc59bd9d50b23648b446748d4b6fb7155a9408685c5b252c32db75d3624",
    }
    source = raw.decode("utf-8")
    assert "import memory_store" in source
    assert "sonder_runtime.adapters.memory_store" not in source


def test_checker_detects_a_violation(tmp_path):
    # Prove the checker is not vacuously green: a domain module importing
    # an adapter must fail.
    #
    # Run against a copy, never the live tree. `check_architecture.py` derives
    # its package root from its own location, so a copied `scripts/` +
    # `sonder_runtime/` under tmp_path exercises the identical code path --
    # without planting a real violation in real source, where a concurrent
    # reader (or a crash before the cleanup) turns this proof into someone
    # else's failing CI gate.
    shutil.copytree(_REPO_ROOT / "sonder_runtime", tmp_path / "sonder_runtime")
    (tmp_path / "scripts").mkdir()
    checker = tmp_path / "scripts" / "check_architecture.py"
    shutil.copy2(_REPO_ROOT / "scripts" / "check_architecture.py", checker)

    # The checker takes its production inventory from `git ls-files`, so the
    # copy has to be a repository with the sources staged. A fresh `git init`
    # here is also owned by the current user, which keeps the copy immune to
    # the ownership refusal that can afflict the real checkout.
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
    ):
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
        "the copied tree must start clean, or the violation below proves "
        "nothing:\n" + clean.stdout + clean.stderr
    )

    violation = (
        tmp_path / "sonder_runtime" / "domain" / "common" / "_test_violation.py"
    )
    violation.write_text(
        "from sonder_runtime.adapters.filesystem import atomic_json\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(checker)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 1
    assert "domain may not import" in result.stdout


@pytest.mark.parametrize(
    "root_module", [
        "context_overflow.py",
        "mmr_rerank.py",
        "reward.py",
        "execution_status.py",
        "process_liveness.py",
        "eval_history.py",
        "sonder_backup.py",
        "sonder_preflight.py",
        "workflow_store.py",
        "sonder_storage.py",
        "model_transport.py",
        "runtime_policy.py",
        "ollama_endpoint.py",
        "embed_cache.py",
        "embeddings.py",
        "npu_contract.py",
        "npu_manifest.py",
        "npu_providers.py",
        "npu_broker.py",
        "npu_worker.py",
        "npu_service.py",
        "activity_tracker.py",
        "sonder_operations_store.py",
        "file_ops.py",
        "workbench.py",
        "sonder_secrets.py",
        "sonder_updates.py",
        "sonder_update_engine.py",
        "sonder_lifecycle.py",
        "sonder_serve.py",
        "sonder_repl.py",
        "sonder_migrations.py",
        "sonder_metrics.py",
    ],
)
def test_checker_rejects_reintroduced_migrated_root(tmp_path, root_module):
    """A completed root migration is a permanent shrink-only boundary."""
    shutil.copytree(_REPO_ROOT / "sonder_runtime", tmp_path / "sonder_runtime")
    (tmp_path / "scripts").mkdir()
    checker = tmp_path / "scripts" / "check_architecture.py"
    shutil.copy2(_REPO_ROOT / "scripts" / "check_architecture.py", checker)

    retired = tmp_path / root_module
    retired.write_text("# retired migration boundary\n", encoding="utf-8")
    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        staged = subprocess.run(
            command, cwd=tmp_path, capture_output=True, text=True, timeout=120,
        )
        if staged.returncode != 0:
            pytest.skip(
                "git is required to stage the isolated copy: %s"
                % (staged.stderr.strip() or staged.stdout.strip())
            )

    result = subprocess.run(
        [sys.executable, str(checker)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 1
    assert f"{root_module}: retired root module was reintroduced" in result.stdout


def test_inspection_adapter_has_an_exact_read_only_legacy_dependency_set():
    path = (
        _REPO_ROOT / "sonder_runtime" / "adapters"
        / "inspection_executor.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolved = {
        node.args[0].value
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_legacy_module"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
    }
    assert resolved == {
        "sonder_runtime.adapters.filesystem.file_ops", "workspace_compare",
    }
    assert not resolved & {
        "archive_tools", "content_digest", "data_query", "dependency_inventory",
        "archive_extract", "archive_create", "data_convert", "json_patch_tool",
        "log_inspect", "project_detect",
        "sqlite_mutate", "text_patch", "workbench",
    }

    packaged = {
        node.args[0].value
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_packaged_module"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
    }
    assert packaged == {
        "archive_tools", "content_digest", "data_query", "dependency_inventory",
        "log_inspect", "project_detect",
    }


def test_preference_use_case_depends_only_on_narrow_application_ports():
    use_case = (
        _REPO_ROOT / "sonder_runtime" / "application" / "preferences"
        / "use_cases.py"
    )
    tree = ast.parse(use_case.read_text(encoding="utf-8"), filename=str(use_case))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports == {"ports.preferences", "ports.tool_executor", "__future__"}

    adapter = (
        _REPO_ROOT / "sonder_runtime" / "adapters"
        / "preference_adapters.py"
    ).read_text(encoding="utf-8")
    assert "import server" not in adapter
    assert "activity_tracker" not in adapter
    assert "archive_" not in adapter
    assert "task_" not in adapter
    assert "checklist_" not in adapter


def test_preference_codec_is_composed_from_its_canonical_adapter():
    from sonder_runtime.adapters.preference_adapters import LegacyPreferenceCodec
    from sonder_runtime.adapters.preference_codec import PreferenceCodecAdapter
    from sonder_runtime.bootstrap.app import build_application

    assert LegacyPreferenceCodec is PreferenceCodecAdapter
    application = build_application(preference_module_provider=lambda: None)
    assert type(application.preferences._codec) is PreferenceCodecAdapter


def test_domain_modules_are_pure():
    # Importing domain modules must not touch the environment, filesystem,
    # or network. The AST checker covers imports; here we prove the
    # modules load without the heavyweight root modules present in
    # sys.modules.
    import importlib

    for name in (
        "sonder_runtime.domain.common.errors",
        "sonder_runtime.domain.common.ids",
        "sonder_runtime.domain.runtime_policy.rules",
    ):
        module = importlib.import_module(name)
        assert module is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "import sqlite3" not in source
        assert "import urllib" not in source
        assert "import subprocess" not in source


def test_task_application_service_has_no_legacy_or_sqlite_dependency():
    service = _REPO_ROOT / "sonder_runtime" / "application" / "tasks" / "use_cases.py"
    source = service.read_text(encoding="utf-8")
    assert "import memory_store" not in source
    assert "import sqlite3" not in source
    assert "import activity_tracker" not in source
    assert "import server" not in source


def test_evaluation_history_application_service_has_no_persistence_dependency():
    service = (
        _REPO_ROOT / "sonder_runtime" / "application"
        / "evaluation_history" / "use_cases.py"
    )
    source = service.read_text(encoding="utf-8")
    assert "evaluation_history_store" not in source
    assert "import eval_history" not in source
    assert "import server" not in source


def test_evaluation_history_root_alias_is_retired():
    module = _architecture_module()
    assert Path("eval_history.py") in module.RETIRED_ROOT_MODULES
    assert "eval_history" not in module.COMPATIBILITY_ROOT_MODULES


def test_production_inventory_ignores_generated_tree_but_catches_tracked_offender(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("app/build/\n", encoding="utf-8")
    offender = repo / "real_offender.py"
    offender.write_text("import memory_store\n", encoding="utf-8")
    generated = repo / "app" / "build" / "local-system" / "server.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("import memory_store\nimport recall\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitignore", "real_offender.py"],
        check=True,
    )

    module = _architecture_module()
    inventory = {
        path.relative_to(repo)
        for path in module.tracked_production_python_files(repo)
    }
    assert inventory == {Path("real_offender.py")}
    assert module.compatibility_import_offenders(
        "memory_store", Path("memory_store.py"), repo
    ) == (Path("real_offender.py"),)
