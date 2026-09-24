"""Opt-in GitHub issue/PR publication for scenario validation evidence."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .runner import ScenarioReport, _redact


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

    def __init__(self, plan: PublishPlan, lock_factory=None):
        key = hashlib.sha256((plan.issue_command[plan.issue_command.index("--repo") + 1] + "\0" + plan.marker).encode()).hexdigest()
        self.path = Path(tempfile.gettempdir()) / ("sonder-scenario-validation-" + key + ".lock")
        self._lock_factory = lock_factory
        self._lock = None

    def __enter__(self):
        self._lock = self._lock_factory(
            self.path, timeout=30, purpose="scenario-validation-publication"
        )
        self._lock.__enter__()
        return self

    def __exit__(self, *_):
        if self._lock is None:
            return
        self._lock.__exit__(*_)


def _body(report: ScenarioReport) -> str:
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


def build_publish_plan(report: ScenarioReport, *, repository: str, branch: str | None = None, base: str = "main", request_pr: bool = False, clean: bool = False, head_sha: str | None = None) -> PublishPlan:
    if not report.marker or not report.commit_sha:
        raise ValueError("issue/PR publication requires an exact commit SHA and marker")
    if request_pr and (not report.passed or not clean or not branch or branch == base or head_sha != report.commit_sha):
        raise ValueError("PR publication requires a clean non-base branch at the report SHA")
    safe_title = re.sub(r"[^A-Za-z0-9 ._:/-]", "?", _redact(report.scenario)).replace("\n", " ")[:100]
    title = f"scenario validation: {safe_title} ({report.result})"
    body = _body(report)
    issue = ("gh", "issue", "create", "--repo", repository, "--title", title, "--body", body)
    pr = None
    if request_pr:
        pr = ("gh", "pr", "create", "--repo", repository, "--head", branch or "", "--base", base, "--title", title, "--body", body)
    return PublishPlan(report.marker, title, body, issue, pr)


class GitHubPublisher:
    """Execute an already validated plan. Dry-run is deliberately default."""

    def __init__(self, *, dry_run: bool = True, adapter: GitHubAdapter | None = None, lock_factory=None):
        self.dry_run = dry_run
        self.adapter = adapter
        self.lock_factory = lock_factory

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
        if self.lock_factory is None:
            raise RuntimeError("a lock provider is required for non-dry-run publication")
        with _PublicationLock(plan, self.lock_factory):
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
        command = ("gh", "issue", "list", "--repo", repo, "--state", "all", "--search", f"{plan.marker} in:body", "--json", "number,url,body", "--limit", "10")
        if self.adapter is None:
            raise RuntimeError("a GitHub adapter is required for non-dry-run publication")
        completed = self.adapter.run(command)
        if completed.returncode:
            raise RuntimeError("gh issue dedupe query failed")
        try:
            value = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh issue dedupe query returned invalid JSON") from exc
        if not isinstance(value, list):
            raise RuntimeError("gh issue dedupe query returned invalid records")
        return [item for item in value if isinstance(item, dict)
                and isinstance(item.get("body"), str)
                and f"<!-- {plan.marker} -->" in item["body"]]

    def _existing_pr(self, plan: PublishPlan) -> list[object]:
        if not plan.pr_command:
            return []
        try:
            repo = plan.pr_command[plan.pr_command.index("--repo") + 1]
            branch = plan.pr_command[plan.pr_command.index("--head") + 1]
        except (ValueError, IndexError):
            raise ValueError("PR command must identify repository and head")
        command = ("gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "all", "--search", f"{plan.marker} in:body", "--json", "number,url,body", "--limit", "10")
        if self.adapter is None:
            raise RuntimeError("a GitHub adapter is required for non-dry-run publication")
        completed = self.adapter.run(command)
        if completed.returncode:
            raise RuntimeError("gh PR dedupe query failed")
        try:
            value = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh PR dedupe query returned invalid JSON") from exc
        if not isinstance(value, list):
            raise RuntimeError("gh PR dedupe query returned invalid records")
        return [item for item in value if isinstance(item, dict)
                and isinstance(item.get("body"), str)
                and f"<!-- {plan.marker} -->" in item["body"]]
