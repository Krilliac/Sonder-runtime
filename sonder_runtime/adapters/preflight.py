"""Host-I/O implementation of the startup preflight contract."""
from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from dataclasses import dataclass  # noqa: F401 - legacy star-import surface
from pathlib import Path
from urllib.parse import urlsplit

import sonder_runtime.adapters.persistence.migrations as sonder_migrations
from sonder_runtime.platform.config import SonderConfig

from ..application.ports.preflight import CheckResult, PreflightReport


def _check_state_directories(config: SonderConfig) -> list[CheckResult]:
    results = []
    home = Path(config.state.home).expanduser()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".sonder-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.append(CheckResult("state_home_writable", True, True, str(home)))
    except OSError as exc:
        results.append(
            CheckResult("state_home_writable", False, True, f"{home}: {exc}")
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
        "disk_space", free >= needed, True, f"free={free} required={needed}"
    )


def _check_schema_versions(config: SonderConfig) -> list[CheckResult]:
    del config
    results = []
    try:
        statuses = sonder_migrations.status_all()
    except Exception as exc:
        return [CheckResult("schema_versions", False, True, str(exc))]
    for store, status in statuses.items():
        required = True
        if status.unknown:
            detail = f"future schema: unknown migrations {list(status.unknown)}"
            ok = False
        elif status.checksum_mismatches:
            detail = "migration history modified: %s" % list(
                status.checksum_mismatches
            )
            ok = False
        elif status.pending:
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
        import sonder_runtime.adapters.runtime_policy as runtime_policy

        policy = runtime_policy.load()
        return CheckResult(
            "runtime_policy",
            True,
            True,
            f"revision={policy.get('revision', 'unknown')}",
        )
    except Exception as exc:
        return CheckResult("runtime_policy", False, True, str(exc))


def _check_ollama_origin(
    origin: str,
    *,
    name: str,
    required: bool,
    timeout: float,
) -> CheckResult:
    url = origin.rstrip("/") + "/api/tags"
    host = urlsplit(origin).hostname or ""
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return CheckResult(
                    name, False, required, f"{host}: HTTP {response.status}"
                )
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("capability response exceeded 1 MiB")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("capability response must be an object")
            models = len(payload.get("models") or [])
            return CheckResult(name, True, required, f"{host}: {models} models")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult(name, False, required, f"{host}: {exc}")


def _check_ollama(config: SonderConfig, *, timeout: float = 5.0) -> CheckResult:
    return _check_ollama_origin(
        config.ollama.url,
        name="ollama",
        required=True,
        timeout=timeout,
    )


def _check_ollama_workers(
    config: SonderConfig, *, timeout: float = 5.0,
) -> list[CheckResult]:
    """Probe optional workers independently so one outage stays degraded."""
    entries = list(enumerate(config.ollama.workers, start=1))
    if not entries:
        return []

    def check(entry) -> CheckResult:
        index, origin = entry
        return _check_ollama_origin(
            origin,
            name="ollama_worker_%d" % index,
            required=False,
            timeout=timeout,
        )

    with ThreadPoolExecutor(max_workers=min(4, len(entries))) as executor:
        return list(executor.map(check, entries))


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
        checks.extend(_check_ollama_workers(config, timeout=ollama_timeout))
    return PreflightReport(checks=tuple(checks))
