"""Run bounded playtest adapters and emit stable, reviewable evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence


class PlaytestAdapter(Protocol):
    def run(self, command: tuple[str, ...], *, cwd: Path, timeout_seconds: float): ...


class EvidenceClass(str, Enum):
    NATURAL = "natural"
    GM_ACCELERATED = "GM-accelerated"
    SEEDED = "seeded"
    STRUCTURAL_ONLY = "structural only"


@dataclass(frozen=True)
class Scenario:
    name: str
    claim: str
    command: tuple[str, ...]
    evidence_class: EvidenceClass = EvidenceClass.STRUCTURAL_ONLY
    timeout_seconds: float = 60.0
    artifact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.claim, str) or not self.name.strip() or not self.claim.strip():
            raise ValueError("scenario name and claim are required")
        if not isinstance(self.command, tuple) or not self.command or any(not isinstance(part, str) or not part.strip() for part in self.command):
            raise ValueError("scenario command must be a non-empty argv sequence")
        if not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0 or self.timeout_seconds > 3600:
            raise ValueError("timeout_seconds must be between 0 and 3600")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "Scenario":
        command = value.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError("scenario command must be a JSON array")
        try:
            evidence = EvidenceClass(str(value.get("evidence_class", EvidenceClass.STRUCTURAL_ONLY.value)))
        except ValueError as exc:
            raise ValueError("unknown evidence_class") from exc
        refs = value.get("artifact_refs", [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise ValueError("artifact_refs must be an array")
        name, claim = value.get("name"), value.get("claim")
        if not isinstance(name, str) or not isinstance(claim, str):
            raise ValueError("scenario name and claim must be strings")
        timeout = value.get("timeout_seconds", 60.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be finite number")
        return cls(
            name=name,
            claim=claim,
            command=tuple(command),
            evidence_class=evidence,
            timeout_seconds=float(timeout),
            artifact_refs=tuple(refs),
        )


@dataclass(frozen=True)
class PlaytestReport:
    scenario: str
    claim: str
    evidence_class: str
    command: tuple[str, ...]
    result: str
    passed: bool
    exit_code: int | None
    stdout: str
    stderr: str
    errors: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    repository: str = ""
    commit_sha: str = ""
    marker: str = ""
    steps_attempted: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "artifact_refs": list(self.artifact_refs),
            "claim": self.claim,
            "command": list(self.command),
            "commit_sha": self.commit_sha,
            "errors": list(self.errors),
            "exit_code": self.exit_code,
            "evidence_class": self.evidence_class,
            "marker": self.marker,
            "passed": self.passed,
            "repository": self.repository,
            "result": self.result,
            "scenario": self.scenario,
            "stderr": self.stderr,
            "stdout": self.stdout,
            "steps_attempted": self.steps_attempted,
        }


def _redact(text: str) -> str:
    """Keep command output useful without persisting common credential shapes."""
    text = re.sub(r"gh[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED_GH_TOKEN]", text)
    text = re.sub(r"(?i)((?:proxy-)?authorization\s*:\s*[^\s]+\s+)[^\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(\b(?:token|password|secret|api[_-]?key)=)[^\s&]+", r"\1[REDACTED]", text)
    return text[:20000]


_SECRET_FLAGS = {"--token", "--password", "--secret", "--api-key", "--authorization"}


def _safe_argv(command: tuple[str, ...]) -> tuple[str, ...]:
    safe: list[str] = []
    redact_next = None
    for part in command:
        if redact_next == "secret":
            safe.append("[REDACTED_ARG]")
            redact_next = None
            continue
        if redact_next == "header":
            if re.match(r"(?i)(?:proxy-)?authorization\s*:\s*[^\s]+\s+", part):
                safe.append("[REDACTED_ARG]")
            else:
                safe.append(_redact(part))
            redact_next = None
            continue
        lower = part.lower()
        if lower in _SECRET_FLAGS:
            safe.append(part)
            redact_next = "secret"
            continue
        if any(lower.startswith(flag + "=") for flag in _SECRET_FLAGS):
            safe.append(part.split("=", 1)[0] + "=[REDACTED_ARG]")
            continue
        if lower in {"-h", "--header"}:
            safe.append(part)
            redact_next = "header"
            continue
        safe.append(_redact(part))
    return tuple(safe)


def _safe_artifact_ref(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._:/#%+~-]{1,512}", value) or "@" in value:
        return "[REDACTED_ARTIFACT]"
    if re.search(r"(?i)(?:token|password|secret|api[_-]?key)=", value):
        return "[REDACTED_ARTIFACT]"
    if re.search(r"gh[pousr]_[A-Za-z0-9_]{20,}", value):
        return "[REDACTED_ARTIFACT]"
    return value


def stable_marker(scenario: str, commit_sha: str) -> str:
    digest = hashlib.sha256(f"{scenario}\0{commit_sha}".encode()).hexdigest()[:16]
    return f"sonder-playtester:{digest}"


class PlaytestRunner:
    def __init__(self, adapter: PlaytestAdapter, *, cwd: str | Path = ".", max_steps: int = 1, max_failures: int = 2):
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or not 1 <= max_steps <= 1000 or not isinstance(max_failures, int) or isinstance(max_failures, bool) or not 1 <= max_failures <= 100:
            raise ValueError("max_steps and max_failures must be positive")
        self.adapter = adapter
        self.cwd = Path(cwd).resolve()
        self.max_steps = max_steps
        self.max_failures = max_failures

    def run(self, scenarios: Iterable[Scenario], *, repository: str = "", commit_sha: str = "") -> list[PlaytestReport]:
        reports: list[PlaytestReport] = []
        failures = 0
        for index, scenario in enumerate(scenarios):
            if index >= self.max_steps:
                break
            report = self._run_one(scenario, repository=repository, commit_sha=commit_sha)
            reports.append(report)
            if not report.passed:
                failures += 1
                if failures >= self.max_failures:
                    break
        return reports

    def _run_one(self, scenario: Scenario, *, repository: str, commit_sha: str) -> PlaytestReport:
        errors: list[str] = []
        completed = self.adapter.run(scenario.command, cwd=self.cwd, timeout_seconds=scenario.timeout_seconds)
        if completed.timed_out:
            result, passed, exit_code = "blocked", False, None
            errors.append(f"command timed out after {scenario.timeout_seconds:g}s")
        elif completed.error_type:
            result, passed, exit_code = "blocked", False, None
            errors.append(f"adapter error: {completed.error_type}")
        else:
            passed = completed.returncode == 0
            result = "passed" if passed else "failed"
            if not passed:
                errors.append(f"command exited with status {completed.returncode}")
            exit_code = completed.returncode
        stdout, stderr = completed.stdout, completed.stderr
        return PlaytestReport(
            scenario=scenario.name,
            claim=scenario.claim,
            evidence_class=scenario.evidence_class.value,
            command=_safe_argv(scenario.command),
            result=result,
            passed=passed,
            exit_code=exit_code,
            stdout=_redact(str(stdout)),
            stderr=_redact(str(stderr)),
            errors=tuple(errors),
            artifact_refs=tuple(_safe_artifact_ref(ref) for ref in scenario.artifact_refs),
            repository=repository,
            commit_sha=commit_sha,
            marker=stable_marker(scenario.name, commit_sha),
            steps_attempted=1,
        )


def write_reports(path: str | Path, reports: Sequence[PlaytestReport]) -> None:
    payload = {"reports": [report.as_dict() for report in reports]}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
