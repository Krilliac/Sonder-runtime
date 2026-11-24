"""Workspace creation, candidate edits, isolation policy, pruning, and formatting."""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, ContextManager

from sonder_runtime.adapters.selfmod.backup_restore import sha256_file
from sonder_runtime.application.selfmod.governance import (
    SENSITIVE_PARTS,
    SENSITIVE_PREFIXES,
)


UNISOLATED_FLAG = "--unisolated"

INVENTORY_EXCLUDED = frozenset({
    ".git", "venv", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", "build", "dist", ".tooling", "workspaces",
    "site-packages",
})


class CandidateIsolationRefused(PermissionError):
    """An operator-driven candidate check cannot run behind an isolation boundary."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def rel_path(root: Path, path: object) -> str:
    """Normalize and confine a repository-relative path inside *root*."""
    raw = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not raw or raw in {".", ".."} or "\x00" in raw:
        raise ValueError("file path must name a repository-relative file")
    candidate = (root / raw).resolve(strict=False)
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("file path escapes authorized repository") from exc
    if any(part in {".git", ".hg", ".svn"} for part in relative.parts):
        raise ValueError("version-control metadata cannot be modified")
    return relative.as_posix()


def is_protected(path: str) -> bool:
    """Return whether *path* matches any protected sensitive prefix or part."""
    lowered = path.lower().replace("\\", "/")
    return any(
        lowered == p.lower() or lowered.startswith(p.lower())
        for p in SENSITIVE_PREFIXES
    ) or any(part in lowered for part in SENSITIVE_PARTS)


def source_fingerprint(
    root: Path, files: Iterable[str], git_status: str = ""
) -> str:
    """Compute SHA-256 over ``git_status`` and declared file hashes."""
    rows = [git_status]
    for rel in sorted(files):
        path = root / rel
        rows.append("%s:%s" % (rel, sha256_file(path) if path.is_file() else "<missing>"))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def dirty_paths(status: str) -> set[str]:
    """Extract normalized dirty paths from ``git status --porcelain=v1`` output."""
    paths: set[str] = set()
    for line in str(status or "").splitlines():
        pieces = line.strip().split(maxsplit=1)
        value = pieces[1] if len(pieces) == 2 else ""
        for item in value.split(" -> "):
            if item:
                paths.add(item.strip('"').replace("\\", "/"))
    return paths


def test_inventory(root: Path) -> list[str]:
    """Return sorted repo-relative ``test_*.py`` files excluding non-project dirs."""
    inventory: list[str] = []
    for path in root.rglob("test_*.py"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if not INVENTORY_EXCLUDED.intersection(rel.parts):
            inventory.append(rel.as_posix())
    return sorted(inventory)


def prepare_workspace(
    run_id: str,
    *,
    get_run_fn: Callable[[str], dict[str, Any]],
    workspaces_root_fn: Callable[[], Path],
    git_info_fn: Callable[[Path], tuple[bool, str | None, str]],
    git_fn: Callable[..., tuple[int, str]],
    phase_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Create an isolated candidate worktree (or copytree fallback) for *run_id*."""
    run = get_run_fn(run_id)
    if run["phase"] != "backed_up":
        raise RuntimeError("workspace preparation requires verified backup")
    root = Path(run["repository_root"])
    destination = workspaces_root_fn() / run_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("candidate workspace already exists")
    git_mode, commit, _ = git_info_fn(root)
    branch = "selfmod/%s" % run_id
    if git_mode and commit:
        code, output = git_fn(
            root, "worktree", "add", "--detach", str(destination), commit, timeout=120
        )
        if code:
            raise RuntimeError("could not create isolated Git worktree: %s" % output)
        code, output = git_fn(destination, "switch", "-c", branch)
        if code:
            git_fn(root, "worktree", "remove", "--force", str(destination))
            raise RuntimeError("could not create selfmod branch: %s" % output)
    else:
        ignore = shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".venv*", "venv*"
        )
        shutil.copytree(root, destination, ignore=ignore)
        branch = ""
    return phase_fn(
        run_id,
        {"backed_up"},
        "editing",
        "workspace",
        "isolated candidate workspace created",
        workspace_path=str(destination),
        branch_name=branch,
    )


def apply_candidate_changes(
    run_id: str,
    changes: Mapping[str, Any],
    *,
    get_run_fn: Callable[[str], dict[str, Any]],
    candidate_path_fn: Callable[[str], Path],
    tx_fn: Callable[..., ContextManager[sqlite3.Connection]],
    event_fn: Callable[[sqlite3.Connection, str | None, str, str], None],
    inspect_diff_fn: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Apply already-authorized file contents inside the isolated workspace."""
    run = get_run_fn(run_id)
    if run["phase"] != "editing":
        raise RuntimeError("candidate edits require editing phase")
    if run["mode"] == "observe":
        raise PermissionError("observe mode cannot edit")
    workspace = candidate_path_fn(run_id)
    allowed = set(run["files"])
    if len(changes) > run["budgets"]["max_files_changed"]:
        raise ValueError("change set exceeds file budget")
    for raw, content in changes.items():
        rel = rel_path(workspace, raw)
        if rel not in allowed:
            raise PermissionError("candidate attempted an unrelated file: %s" % rel)
        if is_protected(rel) and not run["maintenance_authorized"]:
            raise PermissionError("candidate attempted a protected file: %s" % rel)
        target = workspace / rel
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = str(content).encode("utf-8")
        mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else 0o644
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".selfmod-", dir=target.parent
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(tmp_name, mode)
            os.replace(tmp_name, target)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    with tx_fn() as conn:
        event_fn(conn, run_id, "edit", "candidate changed: %s" % ", ".join(sorted(changes)))
    return inspect_diff_fn(run_id)


def workspace_diff(
    run: Mapping[str, Any], *, candidate_path_fn: Callable[[str], Path]
) -> tuple[str, list[str], int, int]:
    """Compute unified diff, changed file list, additions, and deletions for *run*."""
    root = Path(run["repository_root"])
    workspace = candidate_path_fn(str(run["id"]))
    lines: list[str] = []
    changed: list[str] = []
    additions = 0
    deletions = 0
    for rel in run["files"]:
        before_path, after_path = root / rel, workspace / rel
        before = (
            before_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
            if before_path.is_file()
            else []
        )
        after = (
            after_path.read_text(encoding="utf-8", errors="replace").splitlines(True)
            if after_path.is_file()
            else []
        )
        if before == after:
            continue
        changed.append(rel)
        diff = list(
            difflib.unified_diff(before, after, fromfile="a/" + rel, tofile="b/" + rel)
        )
        lines.extend(diff)
        additions += sum(
            1 for line in diff if line.startswith("+") and not line.startswith("+++")
        )
        deletions += sum(
            1 for line in diff if line.startswith("-") and not line.startswith("---")
        )
    return "".join(lines), changed, additions, deletions


def all_candidate_changes(
    run: Mapping[str, Any],
    *,
    candidate_path_fn: Callable[[str], Path],
    git_info_fn: Callable[[Path], tuple[bool, str | None, str]],
    git_fn: Callable[..., tuple[int, str]],
) -> list[str]:
    """Enumerate every file modified or added inside the candidate workspace."""
    root, workspace = Path(run["repository_root"]), candidate_path_fn(str(run["id"]))
    git_mode, _, _ = git_info_fn(workspace)
    if git_mode:
        code1, tracked = git_fn(workspace, "diff", "--name-only", "HEAD")
        code2, untracked = git_fn(workspace, "ls-files", "--others", "--exclude-standard")
        if code1 or code2:
            raise RuntimeError("could not inventory candidate changes")
        return sorted({
            rel
            for line in (tracked + "\n" + untracked).splitlines()
            if line.strip()
            for rel in [line.strip().replace("\\", "/")]
            if not any(
                part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
                for part in Path(rel).parts
            )
            and not rel.endswith((".pyc", ".pyo"))
        })
    paths: set[str] = set()
    for base in (root, workspace):
        for path in base.rglob("*"):
            if path.is_file() and not any(
                part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts
            ):
                paths.add(path.relative_to(base).as_posix())
    changed: list[str] = []
    for rel in paths:
        before, after = root / rel, workspace / rel
        if before.is_file() != after.is_file() or (
            before.is_file() and sha256_file(before) != sha256_file(after)
        ):
            changed.append(rel)
    return sorted(changed)


def discard_candidate_bytecode(workspace: Path) -> None:
    """Remove candidate ``__pycache__`` and ``.pyc/.pyo`` files before testing."""
    for directory in workspace.rglob("__pycache__"):
        if directory.is_symlink():
            raise RuntimeError("candidate bytecode cache is a symlink")
        if directory.is_dir():
            shutil.rmtree(directory)
    for pattern in ("*.pyc", "*.pyo"):
        for path in workspace.rglob(pattern):
            if path.is_symlink():
                raise RuntimeError("candidate bytecode is a symlink")
            if path.is_file():
                path.unlink()


def operator_candidate_isolation(
    *,
    unisolated_requested: bool,
    operator_attended: bool,
    run_id: str = "",
    refusal_fn: Callable[[], str | None],
    get_run_fn: Callable[[str], dict[str, Any]],
    auto_candidate_fn: Callable[[Mapping[str, Any]], bool],
    tx_fn: Callable[..., ContextManager[sqlite3.Connection]],
    event_fn: Callable[[sqlite3.Connection, str | None, str, str], None],
) -> bool:
    """Choose isolation for an operator-driven run: ``True`` isolated, ``False`` not."""
    refusal = refusal_fn()
    if refusal is None:
        choice, detail = True, "candidate checks run under the selected candidate supervisor"
    else:
        remedy = (
            "configure candidate isolation, or, at an attended console, rerun "
            "/selfmod run with %s to accept an unisolated candidate" % UNISOLATED_FLAG
        )
        if not unisolated_requested:
            raise CandidateIsolationRefused(
                "candidate isolation unavailable, no candidate run started: %s; %s"
                % (refusal, remedy),
                reason=refusal,
            )
        if not operator_attended:
            raise CandidateIsolationRefused(
                "candidate isolation unavailable and %s requires an attended console "
                "operator; HTTP, MCP and unattended callers cannot run a candidate "
                "unisolated: %s" % (UNISOLATED_FLAG, refusal),
                reason=refusal,
            )
        if os.environ.get("SELFMOD_LOW_INTEGRITY") == "1":
            raise CandidateIsolationRefused(
                "candidate isolation unavailable and SELFMOD_LOW_INTEGRITY=1 requires "
                "isolated candidate checks in this process: %s" % refusal,
                reason=refusal,
            )
        if run_id and auto_candidate_fn(get_run_fn(run_id)):
            raise CandidateIsolationRefused(
                "candidate isolation unavailable and auto-low-risk candidate tests "
                "require isolation: %s" % refusal,
                reason=refusal,
            )
        choice = False
        detail = "attended console operator accepted an unisolated candidate (%s): %s" % (
            UNISOLATED_FLAG, refusal[:300]
        )
    if run_id:
        with tx_fn() as conn:
            event_fn(conn, run_id, "isolation", detail)
    return choice


def evaluator_truth_paths(
    run_id: str,
    extra: Iterable[str | Path] = (),
    *,
    backup_dir_fn: Callable[[str], Path],
) -> tuple[str, ...]:
    """Return evaluator truth paths every candidate check must leave untouched."""
    paths = [str(path) for path in extra]
    bundle = backup_dir_fn(run_id)
    if bundle.is_dir():
        paths.append(str(bundle))
        for name in ("manifest.json", "manifest.sha256"):
            if (bundle / name).is_file():
                paths.append(str(bundle / name))
    return tuple(dict.fromkeys(paths))


def prune_workspaces(
    retention_days: int | None = None,
    *,
    workspaces_root_fn: Callable[[], Path],
    settings_fn: Callable[[], Mapping[str, Any]],
    connect_fn: Callable[[], sqlite3.Connection],
    decode_run_fn: Callable[[sqlite3.Row], dict[str, Any] | None],
    git_fn: Callable[..., tuple[int, str]],
    active_phases: frozenset[str] | set[str],
) -> list[str]:
    """Remove expired scratch workspaces (and Git worktrees/branches) for finished runs."""
    root = workspaces_root_fn()
    if not root.exists():
        return []
    days = int(
        retention_days if retention_days is not None else settings_fn()["retention_days"]
    )
    cutoff = time.time() - max(1, days) * 86400
    with connect_fn() as conn:
        rows = conn.execute("SELECT * FROM selfmod_runs").fetchall()
    runs = {row["id"]: decode_run_fn(row) for row in rows}
    removed: list[str] = []
    for path in root.glob("selfmod-*"):
        run = runs.get(path.name)
        if run is None or run["phase"] in active_phases:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        repo = run.get("repository_root") or ""
        if repo and Path(repo).exists():
            code, _ = git_fn(Path(repo), "worktree", "remove", "--force", str(path))
            if code:
                with contextlib.suppress(OSError):
                    shutil.rmtree(path)
            git_fn(Path(repo), "worktree", "prune")
            branch = run.get("branch_name") or ""
            if branch:
                git_fn(Path(repo), "branch", "-D", branch)
        else:
            with contextlib.suppress(OSError):
                shutil.rmtree(path)
        if not path.exists():
            removed.append(path.name)
    return removed


def format_status(data: Mapping[str, Any]) -> str:
    """Format selfmod status summary for operator output."""
    lines = [
        "Sonder self-modification",
        "  enabled: %s | mode: %s" % ("yes" if data["enabled"] else "no", data["mode"]),
        "  active: %d | deployed: %d | rollback points: %d" % (
            data["active"], data["deployed"], data["rollback_points"]
        ),
        "  backup root: %s" % data["backup_root"],
    ]
    shown = list(data["runs"][:10])
    for run in shown:
        lines.append(
            "  %s  %-18s %-8s %s"
            % (run["id"], run["phase"], run["risk"], run["objective"][:80])
        )
    total = int(data.get("total_runs", len(shown)))
    if total > len(shown):
        lines.append(
            "  (showing %d of %d run(s); counts above are over all)"
            % (len(shown), total)
        )
    return "\n".join(lines)


def format_run(run: Mapping[str, Any]) -> str:
    """Format one selfmod run record for operator display."""
    return "\n".join([
        "%s | phase=%s | mode=%s | risk=%s"
        % (run["id"], run["phase"], run["mode"], run["risk"]),
        "objective: %s" % run["objective"],
        "problem: %s" % run["problem"],
        "files: %s" % (", ".join(run["files"]) or "(none declared)"),
        "backup: %s" % (run.get("backup_manifest") or "not created"),
        "approval: %s"
        % (
            run.get("approved_by")
            or ("required" if run["approval_required"] else "host low-risk eligible")
        ),
        "workspace: %s" % (run.get("workspace_path") or "not created"),
        "last error: %s" % (run.get("last_error") or "none"),
    ])


def parse_plan_text(text: object) -> tuple[str, list[str], list[str]]:
    """Parse ``/selfmod plan objective --files a.py,b.py --tests cmd`` safely."""
    cleaned = str(text or "").strip()
    marker = " --files "
    if marker not in cleaned:
        return cleaned, [], []
    objective, tail = cleaned.split(marker, 1)
    tests: list[str] = []
    if " --tests " in tail:
        files_text, tests_text = tail.split(" --tests ", 1)
        tests = [item.strip() for item in tests_text.split(";;") if item.strip()]
    else:
        files_text = tail
    files = [item.strip() for item in files_text.split(",") if item.strip()]
    return objective.strip(), files, tests
