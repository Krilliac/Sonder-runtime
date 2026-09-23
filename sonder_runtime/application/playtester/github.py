"""Opt-in GitHub issue/PR publication for playtest evidence."""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .runner import PlaytestReport, _redact


class GitHubAdapter(Protocol):
    def run(self, command: tuple[str, ...]): ...


@dataclass(frozen=True)
class PublishPlan:
    marker: str
    issue_title: str
    issue_body: str
    issue_command: tuple[str, ...]
    pr_command: tuple[str, ...] | None
    warnings: tuple[str, ...] = ()


class _PublicationLock:
    """Small cross-process lock for marker lookup plus publication."""

    def __init__(self, plan: PublishPlan):
        key = hashlib.sha256((plan.issue_command[plan.issue_command.index("--repo") + 1] + "\0" + plan.marker).encode()).hexdigest()
        self.path = Path(tempfile.gettempdir()) / ("sonder-playtester-" + key + ".lock")
        self.handle = None

    def __enter__(self):
        self.path.touch(exist_ok=True)
        self.handle = self.path.open("r+b")
        deadline = time.monotonic() + 30
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise RuntimeError("timed out waiting for playtest publication lock")
                time.sleep(0.05)

    def __exit__(self, *_):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def _body(report: PlaytestReport) -> str:
    # Publish a concise, curated summary. Raw adapter output remains local evidence.
    scenario = re.sub(r"[^A-Za-z0-9 ._:/-]", "?", report.scenario)[:120]
    claim = _safe_text(report.claim, 240)
    errors = "; ".join(_safe_text(error, 160) for error in report.errors) or "none"
    refs = [ref for ref in report.artifact_refs if _safe_ref(ref)]
    return "\n".join([
        f"<!-- {report.marker} -->",
        f"Scenario: {scenario}",
        f"Evidence class: {report.evidence_class}",
        f"Claim: {claim}",
        f"Result: {report.result}",
        f"Exit code: {report.exit_code if report.exit_code is not None else 'none'}",
        f"Commit: {report.commit_sha or 'unknown'}",
        f"Repository: {report.repository or 'unknown'}",
        f"Errors: {errors}",
        f"Artifacts: {', '.join(refs) or 'none'}",
    ])


def _safe_text(value: str, limit: int) -> str:
    return re.sub(r"[\r\n]", " ", _redact(value))[:limit]


def _safe_ref(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._:/#%+~-]{1,512}", value)) and "@" not in value and not re.search(r"gh[pousr]_[A-Za-z0-9_]{20,}", value)


def build_publish_plan(report: PlaytestReport, *, repository: str, branch: str | None = None, base: str = "main", request_pr: bool = False, clean: bool = False, head_sha: str | None = None) -> PublishPlan:
    if not report.marker or not report.commit_sha:
        raise ValueError("issue/PR publication requires an exact commit SHA and marker")
    if request_pr and (not report.passed or not clean or not branch or branch == base or head_sha != report.commit_sha):
        raise ValueError("PR publication requires a clean non-base branch at the report SHA")
    safe_title = re.sub(r"[^A-Za-z0-9 ._:/-]", "?", _redact(report.scenario)).replace("\n", " ")[:100]
    title = f"playtest: {safe_title} ({report.result})"
    body = _body(report)
    issue = ("gh", "issue", "create", "--repo", repository, "--title", title, "--body", body)
    pr = None
    if request_pr:
        pr = ("gh", "pr", "create", "--repo", repository, "--head", branch or "", "--base", base, "--title", title, "--body", body)
    return PublishPlan(report.marker, title, body, issue, pr)


class GitHubPublisher:
    """Execute an already validated plan. Dry-run is deliberately default."""

    def __init__(self, *, dry_run: bool = True, adapter: GitHubAdapter | None = None):
        self.dry_run = dry_run
        self.adapter = adapter

    def publish(self, plan: PublishPlan, *, create_issue: bool = True, create_pr: bool = False) -> dict[str, object]:
        commands = []
        if create_issue:
            commands.append(plan.issue_command)
        if create_pr:
            if plan.pr_command is None:
                raise ValueError("publish plan has no PR command")
            commands.append(plan.pr_command)
        if self.dry_run:
            return {"dry_run": True, "operations": ["issue" if command == plan.issue_command else "pr" for command in commands], "title": plan.issue_title, "marker": plan.marker}
        if self.adapter is None:
            raise RuntimeError("a GitHub adapter is required for non-dry-run publication")
        with _PublicationLock(plan):
            results = []
            existing = self._existing_issue(plan) if create_issue else []
            existing_pr = self._existing_pr(plan) if create_pr else []
            if existing:
                commands = [command for command in commands if command != plan.issue_command]
            if existing_pr and plan.pr_command:
                commands = [command for command in commands if command != plan.pr_command]
            for command in commands:
                completed = self.adapter.run(command)
                results.append({"command": list(command[:4]), "returncode": completed.returncode, "stdout": _redact(completed.stdout[-2000:]), "stderr": _redact(completed.stderr[-2000:])})
                if completed.returncode:
                    raise RuntimeError(f"gh command failed with status {completed.returncode}")
            return {"dry_run": False, "results": results, "marker": plan.marker, "deduplicated_issue": bool(existing), "deduplicated_pr": bool(existing_pr)}

    def _existing_issue(self, plan: PublishPlan) -> list[object]:
        try:
            repo = plan.issue_command[plan.issue_command.index("--repo") + 1]
        except (ValueError, IndexError):
            raise ValueError("issue command must identify a repository")
        command = ("gh", "issue", "list", "--repo", repo, "--search", f"{plan.marker} in:body", "--json", "number,url", "--limit", "10")
        if self.adapter is None:
            raise RuntimeError("a GitHub adapter is required for non-dry-run publication")
        completed = self.adapter.run(command)
        if completed.returncode:
            raise RuntimeError("gh issue dedupe query failed")
        try:
            value = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh issue dedupe query returned invalid JSON") from exc
        return value if isinstance(value, list) else []

    def _existing_pr(self, plan: PublishPlan) -> list[object]:
        if not plan.pr_command:
            return []
        try:
            repo = plan.pr_command[plan.pr_command.index("--repo") + 1]
            branch = plan.pr_command[plan.pr_command.index("--head") + 1]
        except (ValueError, IndexError):
            raise ValueError("PR command must identify repository and head")
        command = ("gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open", "--search", f"{plan.marker} in:body", "--json", "number,url", "--limit", "10")
        if self.adapter is None:
            raise RuntimeError("a GitHub adapter is required for non-dry-run publication")
        completed = self.adapter.run(command)
        if completed.returncode:
            raise RuntimeError("gh PR dedupe query failed")
        try:
            value = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh PR dedupe query returned invalid JSON") from exc
        return value if isinstance(value, list) else []
