"""Startup preflight: every mandatory check passes before any listener opens.

SPEC-2 readiness requirements (section 4).  Each check reports
independently so an operator sees the complete picture in one run, and
``PreflightReport.ok`` is the single gate the entry point consults before
binding a socket or accepting work.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import sonder_migrations
from sonder_config import SonderConfig


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    required: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    @property
    def degraded(self) -> bool:
        return self.ok and any(not c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "degraded": self.degraded,
            "checks": [c.as_dict() for c in self.checks],
        }


def _check_state_directories(config: SonderConfig) -> list[CheckResult]:
    results = []
    home = Path(config.state.home).expanduser()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".sonder-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.append(
            CheckResult("state_home_writable", True, True, str(home))
        )
    except OSError as exc:
        results.append(
            CheckResult(
                "state_home_writable", False, True, f"{home}: {exc}"
            )
        )
    for root in config.state.workspace_roots:
        path = Path(root).expanduser()
        ok = path.is_dir() and os.access(path, os.W_OK)
        results.append(
            CheckResult(
                f"workspace_root:{root}",
                ok,
                True,
                str(path) if ok else f"{path} is not a writable directory",
            )
        )
    return results


def _check_disk_space(config: SonderConfig) -> CheckResult:
    home = Path(config.state.home).expanduser()
    target = home if home.exists() else home.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError as exc:
        return CheckResult("disk_space", False, True, f"{target}: {exc}")
    needed = config.state.minimum_free_disk_bytes
    return CheckResult(
        "disk_space",
        free >= needed,
        True,
        f"free={free} required={needed}",
    )


def _check_schema_versions(config: SonderConfig) -> list[CheckResult]:
    del config
    results = []
    try:
        statuses = sonder_migrations.status_all()
    except Exception as exc:  # a broken store must fail preflight, not crash it
        return [CheckResult("schema_versions", False, True, str(exc))]
    for store, status in statuses.items():
        required = True
        if status.unknown:
            detail = f"future schema: unknown migrations {list(status.unknown)}"
            ok = False
        elif status.checksum_mismatches:
            detail = (
                "migration history modified: "
                f"{list(status.checksum_mismatches)}"
            )
            ok = False
        elif status.pending:
            # Pending migrations are applied by `serve`/`migrate` during the
            # MIGRATING phase; preflight reports them as degraded rather
            # than blocking the ExecStartPre gate of a fresh release.
            detail = f"pending migrations: {list(status.pending)}"
            ok = False
            required = False
        else:
            detail = f"{len(status.applied)} applied"
            ok = True
        results.append(CheckResult(f"schema:{store}", ok, required, detail))
    return results


def _check_runtime_policy() -> CheckResult:
    try:
        import runtime_policy

        policy = runtime_policy.load()
        detail = f"revision={policy.get('revision', 'unknown')}"
        return CheckResult("runtime_policy", True, True, detail)
    except Exception as exc:
        return CheckResult("runtime_policy", False, True, str(exc))


def _check_ollama(config: SonderConfig, *, timeout: float = 5.0) -> CheckResult:
    url = config.ollama.url.rstrip("/") + "/api/tags"
    host = urlsplit(config.ollama.url).hostname or ""
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return CheckResult(
                    "ollama", False, True, f"{host}: HTTP {response.status}"
                )
            payload = json.loads(response.read(1_048_576).decode("utf-8"))
            models = len(payload.get("models") or [])
            return CheckResult("ollama", True, True, f"{host}: {models} models")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult("ollama", False, True, f"{host}: {exc}")


def run_preflight(
    config: SonderConfig,
    *,
    check_ollama: bool = True,
    ollama_timeout: float = 5.0,
) -> PreflightReport:
    checks: list[CheckResult] = []
    checks.extend(_check_state_directories(config))
    checks.append(_check_disk_space(config))
    checks.extend(_check_schema_versions(config))
    checks.append(_check_runtime_policy())
    if check_ollama:
        checks.append(_check_ollama(config, timeout=ollama_timeout))
    return PreflightReport(checks=tuple(checks))
