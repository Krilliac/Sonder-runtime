#!/usr/bin/env python3
"""Run bounded scenario validation scenarios and optionally prepare GitHub publication."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.application.scenario_validation import (  # noqa: E402
    EvidenceClass,
    GitHubPublisher,
    ScenarioValidationRunner,
    Scenario,
    build_publish_plan,
    write_reports,
)
from sonder_runtime.adapters.scenario_validation import GhCliAdapter, ProcessAdapter  # noqa: E402
from sonder_runtime.adapters.filesystem.durable_locks import exclusive_file_lock  # noqa: E402


def _scenarios(args: argparse.Namespace) -> list[Scenario]:
    if args.catalog:
        data = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("catalog must contain a JSON array")
        return [Scenario.from_mapping(item) for item in data]
    command = json.loads(args.command_json) if args.command_json else args.command
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError("--command-json must be a JSON string array")
    return [Scenario(args.name, args.claim, tuple(command), EvidenceClass(args.evidence_class), args.timeout, tuple(args.artifact))]


def _git(cwd: str, *command: str) -> tuple[int, str]:
    completed = subprocess.run(["git", *command], cwd=cwd, capture_output=True, text=True, check=False)
    return completed.returncode, completed.stdout.strip()


def _remote_repo(url: str) -> str:
    value = url.strip().removesuffix("/").removesuffix(".git")
    ssh = re.fullmatch(r"git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", value)
    https = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", value)
    match = ssh or https
    if not match:
        raise RuntimeError("origin is not a recognized github.com repository")
    return f"{match.group(1)}/{match.group(2)}"


def _validate_real_pr_state(cwd: str, repository: str, branch: str, expected_sha: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch or "@{" in branch:
        raise RuntimeError("invalid branch name")
    checks = {}
    for name, command in (
        ("HEAD", ("rev-parse", "HEAD")),
        ("branch", ("branch", "--show-current")),
        ("status", ("status", "--porcelain")),
        ("origin", ("remote", "get-url", "origin")),
        ("remote-head", ("ls-remote", "--heads", "origin", f"refs/heads/{branch}")),
    ):
        code, output = _git(cwd, *command)
        if code:
            raise RuntimeError(f"git {name} check failed")
        checks[name] = output
    if checks["HEAD"] != expected_sha or checks["branch"] != branch or checks["status"]:
        raise RuntimeError("working tree changed during scenario validation or is not clean")
    if _remote_repo(checks["origin"]).casefold() != repository.casefold():
        raise RuntimeError("origin repository does not match publication repository")
    remote_fields = checks["remote-head"].split()
    if len(remote_fields) != 2 or remote_fields[1] != f"refs/heads/{branch}" or remote_fields[0] != expected_sha:
        raise RuntimeError("remote branch SHA does not match tested SHA")


def _validate_issue_state(cwd: str, repository: str, expected_sha: str) -> None:
    """Prevent publishing failure evidence after the tested checkout changed."""
    head_code, head = _git(cwd, "rev-parse", "HEAD")
    status_code, status = _git(cwd, "status", "--porcelain")
    origin_code, origin = _git(cwd, "remote", "get-url", "origin")
    if head_code or status_code or origin_code or head != expected_sha or status:
        raise RuntimeError("working tree changed during scenario validation or is not clean")
    if _remote_repo(origin).casefold() != repository.casefold():
        raise RuntimeError("origin repository does not match publication repository")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", help="JSON array of scenarios")
    parser.add_argument("--trusted-local", action="store_true", help="allow commands from a local, operator-trusted catalog")
    parser.add_argument("--name", default="explicit-scenario-validation")
    parser.add_argument("--claim", default="The adapter command completes successfully")
    parser.add_argument("--evidence-class", default="structural only", choices=[e.value for e in EvidenceClass])
    parser.add_argument("--command", nargs="+", help="argv without flag-prefixed arguments; use --command-json for flags")
    parser.add_argument("--command-json", help="argv as a JSON string array; supports arguments beginning with '-'")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--max-steps", type=int, default=1)
    parser.add_argument("--max-failures", type=int, default=2)
    parser.add_argument("--output", help="evidence JSON path; defaults outside the worktree")
    parser.add_argument("--publish-repo")
    parser.add_argument("--publish", action="store_true", help="opt in to gh; without this only a dry-run plan is emitted")
    parser.add_argument("--pr", action="store_true")
    parser.add_argument("--branch")
    parser.add_argument("--base", default="main")
    args = parser.parse_args(argv)
    if not (1 <= args.max_steps <= 1000) or not (1 <= args.max_failures <= 100):
        parser.error("max-steps must be 1..1000 and max-failures must be 1..100")
    if not math.isfinite(args.timeout) or not (0 < args.timeout <= 3600):
        parser.error("timeout must be finite and between 0 and 3600")
    if args.pr and not args.publish_repo:
        parser.error("--pr requires --publish-repo")
    if args.pr and not args.branch:
        parser.error("--pr requires --branch")
    if args.catalog and not args.trusted_local:
        parser.error("--catalog requires --trusted-local; catalog commands execute with local user privileges")
    if not args.catalog and not args.command and not args.command_json:
        parser.error("--command or --command-json is required unless --catalog is supplied")
    scenarios = _scenarios(args)
    sha_code, sha = _git(args.cwd, "rev-parse", "HEAD")
    if sha_code or not sha:
        raise RuntimeError("could not resolve tested HEAD SHA")
    repo = args.publish_repo or ""
    reports = ScenarioValidationRunner(ProcessAdapter(), cwd=args.cwd, max_steps=args.max_steps, max_failures=args.max_failures).run(scenarios, repository=repo, commit_sha=sha)
    output_path = Path(args.output) if args.output else Path(tempfile.mkdtemp(prefix="sonder-scenario-validation-")) / "evidence.json"
    write_reports(output_path, reports)
    output: dict[str, object] = {"reports": [report.as_dict() for report in reports], "output": str(output_path.resolve())}
    if args.publish_repo:
        publications = []
        for report in reports:
            create_issue = not report.passed
            create_pr = bool(report.passed and args.pr)
            if not create_issue and not create_pr:
                continue
            final_code, final_sha = _git(args.cwd, "rev-parse", "HEAD")
            branch_code, final_branch = _git(args.cwd, "branch", "--show-current")
            status_code, status = _git(args.cwd, "status", "--porcelain")
            if final_code or branch_code or status_code:
                raise RuntimeError("could not verify final git state")
            output_inside_worktree = output_path.resolve().is_relative_to(Path(args.cwd).resolve())
            clean = not bool(status) and not output_inside_worktree
            if args.pr and final_branch != args.branch:
                raise RuntimeError("PR branch changed during scenario validation")
            if args.publish:
                _validate_issue_state(args.cwd, args.publish_repo, report.commit_sha)
                if args.pr:
                    _validate_real_pr_state(args.cwd, args.publish_repo, final_branch, report.commit_sha)
            plan = build_publish_plan(report, repository=args.publish_repo, branch=final_branch, base=args.base, request_pr=create_pr, clean=clean, head_sha=final_sha)
            publications.append(GitHubPublisher(dry_run=not args.publish, adapter=GhCliAdapter() if args.publish else None, lock_factory=exclusive_file_lock).publish(plan, create_issue=create_issue, create_pr=create_pr))
        output["publications"] = publications
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(report.passed for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
