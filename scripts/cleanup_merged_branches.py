"""Audit or safely remove merged Git branches and their clean worktrees."""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

DEFAULT_PROTECTED = ("main", "master", "develop", "development", "release/*")
REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class GitError(RuntimeError):
    """A sanitized Git command failure that cannot expose command output."""


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"{argv[0]} command could not run") from exc
    if check and result.returncode != 0:
        raise GitError(f"{argv[0]} command failed with exit {result.returncode}")
    return result


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return _run(("git", "-C", str(repo), *args), cwd=repo, check=check)


def resolve_repo(raw: Path) -> Path:
    supplied = raw.expanduser().resolve(strict=True)
    if not supplied.is_dir():
        raise ValueError("--repo must name an existing directory")
    result = _git(supplied, "rev-parse", "--show-toplevel")
    discovered = Path(result.stdout.decode("utf-8", "strict").strip()).resolve(strict=True)
    if discovered != supplied:
        raise ValueError("--repo must be the exact resolved Git repository root")
    return supplied


@dataclass
class Worktree:
    path: Path
    branch: str | None = None
    head: str | None = None
    bare: bool = False
    detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass
class Candidate:
    branch: str
    local_tip: str
    remote_tip: str | None
    worktree: Worktree | None
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "eligible": self.eligible,
            "evidence": self.evidence,
            "local_tip": self.local_tip,
            "reasons": sorted(self.reasons),
            "remote_tip": self.remote_tip,
            "worktree": str(self.worktree.path) if self.worktree else None,
        }


def list_worktrees(repo: Path) -> list[Worktree]:
    data = _git(repo, "worktree", "list", "--porcelain", "-z").stdout
    records: list[Worktree] = []
    current: dict[str, bytes] = {}
    flags: set[str] = set()
    for token in data.split(b"\0"):
        if not token:
            if current:
                raw_path = Path(current["worktree"].decode("utf-8", "strict"))
                path = raw_path.resolve(strict=raw_path.exists())
                branch_raw = current.get("branch")
                branch = branch_raw.decode("utf-8", "strict") if branch_raw else None
                if branch and branch.startswith("refs/heads/"):
                    branch = branch.removeprefix("refs/heads/")
                records.append(Worktree(
                    path=path,
                    branch=branch,
                    head=current.get("HEAD", b"").decode("ascii", "strict") or None,
                    bare="bare" in flags,
                    detached="detached" in flags,
                    locked="locked" in flags or "locked" in current,
                    prunable="prunable" in flags or "prunable" in current,
                ))
                current = {}
                flags = set()
            continue
        key, separator, value = token.partition(b" ")
        name = key.decode("ascii", "strict")
        if separator:
            current[name] = value
        else:
            flags.add(name)
    return records


def list_local_branches(repo: Path) -> dict[str, str]:
    fmt = "%(refname:short)%00%(objectname)%00"
    data = _git(repo, "for-each-ref", f"--format={fmt}", "refs/heads/").stdout
    fields = [field for field in data.split(b"\0") if field.strip()]
    if len(fields) % 2:
        raise GitError("git branch inventory returned malformed data")
    branches = {}
    for index in range(0, len(fields), 2):
        name = fields[index].decode("utf-8", "strict").strip()
        tip = fields[index + 1].decode("ascii", "strict").strip()
        branches[name] = tip
    return branches


def _ref_tip(repo: Path, ref: str) -> str | None:
    result = _git(repo, "rev-parse", "--verify", ref, check=False)
    if result.returncode != 0:
        return None
    tip = result.stdout.decode("ascii", "strict").strip()
    return tip if len(tip) == 40 else None


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if result.returncode not in (0, 1):
        raise GitError("git ancestry check failed")
    return result.returncode == 0


def _is_dirty(worktree: Worktree) -> bool:
    result = _run(
        ("git", "-C", str(worktree.path), "status", "--porcelain=v1", "-z"),
        cwd=worktree.path,
    )
    return bool(result.stdout)


def _protected(branch: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in patterns)


def _validate_names(repo: Path, remote: str, main_branch: str) -> None:
    if not REMOTE_RE.fullmatch(remote) or ".." in remote or "//" in remote:
        raise ValueError("remote name contains unsupported characters")
    remotes = {
        line.strip()
        for line in _git(repo, "remote").stdout.decode("utf-8", "strict").splitlines()
    }
    if remote not in remotes:
        raise ValueError(f"configured remote is missing: {remote}")
    result = _git(repo, "check-ref-format", f"refs/heads/{main_branch}", check=False)
    if result.returncode != 0:
        raise ValueError("main branch name is not a valid Git ref")


def _merged_pr(repo: Path, branch: str, main_branch: str) -> bool | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    result = _run(
        (
            gh, "pr", "view", branch, "--json",
            "state,mergedAt,baseRefName,headRefName",
        ),
        cwd=repo,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        payload.get("state") == "MERGED"
        and payload.get("mergedAt")
        and payload.get("baseRefName") == main_branch
        and payload.get("headRefName") == branch
    )


def audit(
    repo: Path,
    *,
    remote: str = "origin",
    main_branch: str = "main",
    protected: Sequence[str] = DEFAULT_PROTECTED,
    selected: Sequence[str] = (),
    require_merged_pr: bool = False,
) -> list[Candidate]:
    _validate_names(repo, remote, main_branch)
    worktrees = list_worktrees(repo)
    by_branch = {item.branch: item for item in worktrees if item.branch}
    current_path = repo.resolve()
    branches = list_local_branches(repo)
    names = sorted(set(selected) if selected else branches)
    main_ref = f"refs/remotes/{remote}/{main_branch}"
    if _ref_tip(repo, main_ref) is None:
        raise ValueError(f"required main ref is missing: {remote}/{main_branch}")

    candidates = []
    for branch in names:
        if branch not in branches:
            candidates.append(Candidate(
                branch=branch,
                local_tip="",
                remote_tip=None,
                worktree=None,
                reasons=["LOCAL_BRANCH_MISSING"],
            ))
            continue
        worktree = by_branch.get(branch)
        remote_ref = f"refs/remotes/{remote}/{branch}"
        remote_tip = _ref_tip(repo, remote_ref)
        local_merged = _is_ancestor(repo, f"refs/heads/{branch}", main_ref)
        remote_merged = remote_tip is None or _is_ancestor(repo, remote_ref, main_ref)
        candidate = Candidate(
            branch=branch,
            local_tip=branches[branch],
            remote_tip=remote_tip,
            worktree=worktree,
            evidence={
                "local_tip_in_remote_main": local_merged,
                "remote_branch_present": remote_tip is not None,
                "remote_tip_in_remote_main": remote_merged if remote_tip else None,
            },
        )
        if _protected(branch, protected):
            candidate.reasons.append("PROTECTED_BRANCH")
        if not local_merged:
            candidate.reasons.append("LOCAL_TIP_NOT_MERGED")
        if remote_tip and not remote_merged:
            candidate.reasons.append("REMOTE_TIP_NOT_MERGED")
        if worktree:
            if worktree.path == current_path:
                candidate.reasons.append("CURRENT_WORKTREE")
            if worktree.locked:
                candidate.reasons.append("LOCKED_WORKTREE")
            if worktree.prunable or not worktree.path.is_dir():
                candidate.reasons.append("MISSING_WORKTREE")
            elif _is_dirty(worktree):
                candidate.reasons.append("DIRTY_WORKTREE")
        if require_merged_pr:
            pr_merged = _merged_pr(repo, branch, main_branch)
            candidate.evidence["merged_pr"] = pr_merged
            if pr_merged is None:
                candidate.reasons.append("PR_EVIDENCE_UNAVAILABLE")
            elif not pr_merged:
                candidate.reasons.append("MERGED_PR_NOT_CONFIRMED")
        candidates.append(candidate)
    return candidates


def cleanup(
    repo: Path,
    candidates: Sequence[Candidate],
    *,
    remote: str,
) -> list[dict[str, Any]]:
    results = []
    for candidate in candidates:
        if not candidate.eligible:
            results.append({"branch": candidate.branch, "status": "refused"})
            continue
        actions = []
        if candidate.remote_tip:
            result = _git(repo, "push", remote, "--delete", "--", candidate.branch, check=False)
            if result.returncode != 0:
                results.append({
                    "branch": candidate.branch,
                    "status": "failed_remote_delete",
                })
                continue
            actions.append("remote_branch_deleted")
        if candidate.worktree:
            result = _git(
                repo, "worktree", "remove", "--", str(candidate.worktree.path), check=False
            )
            if result.returncode != 0:
                results.append({
                    "actions": actions,
                    "branch": candidate.branch,
                    "status": "failed_worktree_remove",
                })
                continue
            actions.append("worktree_removed")
        result = _git(repo, "branch", "-d", "--", candidate.branch, check=False)
        if result.returncode != 0:
            results.append({
                "actions": actions,
                "branch": candidate.branch,
                "status": "failed_local_delete",
            })
            continue
        actions.append("local_branch_deleted")
        results.append({"actions": actions, "branch": candidate.branch, "status": "deleted"})
    _git(repo, "worktree", "prune")
    return results


def build_report(
    repo: Path,
    candidates: Sequence[Candidate],
    *,
    applied: bool,
    results: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "applied": applied,
        "candidates": [item.as_dict() for item in sorted(candidates, key=lambda item: item.branch)],
        "repo": str(repo),
        "results": sorted(results, key=lambda item: item["branch"]),
        "schema": 1,
    }


def render_plain(report: dict[str, Any]) -> str:
    mode = "APPLY" if report["applied"] else "DRY-RUN"
    lines = [f"merged branch cleanup: {mode}", f"repo: {report['repo']}"]
    for item in report["candidates"]:
        state = "eligible" if item["eligible"] else "refused:" + ",".join(item["reasons"])
        lines.append(f"{item['branch']}: {state} worktree={item['worktree'] or '-'}")
    for item in report["results"]:
        lines.append(f"result {item['branch']}: {item['status']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--branch", action="append", default=[])
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--main", default="main")
    parser.add_argument("--protected", action="append", default=[])
    parser.add_argument("--require-merged-pr", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not args.branch:
        parser.error("--apply requires at least one explicit --branch")
    try:
        repo = resolve_repo(args.repo)
        _validate_names(repo, args.remote, args.main)
        if args.apply:
            _git(repo, "fetch", "--prune", args.remote)
        protected = (*DEFAULT_PROTECTED, *args.protected)
        candidates = audit(
            repo,
            remote=args.remote,
            main_branch=args.main,
            protected=protected,
            selected=args.branch,
            require_merged_pr=args.require_merged_pr,
        )
        results = cleanup(repo, candidates, remote=args.remote) if args.apply else []
        report = build_report(repo, candidates, applied=args.apply, results=results)
    except (GitError, OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_plain(report))
    if args.apply and any(item["status"] != "deleted" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
