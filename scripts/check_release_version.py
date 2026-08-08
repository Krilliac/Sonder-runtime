"""Validate Sonder's runtime, Flutter, and app-tag release version contract."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORE_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
RUNTIME_RE = re.compile(rf"^(?P<core>{CORE_PATTERN})(?:\.dev(?P<dev>[0-9]+))?$")
APP_RE = re.compile(rf"^(?P<core>{CORE_PATTERN})\+(?P<build>[1-9][0-9]*)$")
TAG_RE = re.compile(rf"^app-v(?P<core>{CORE_PATTERN})$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


def parse_runtime_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ValueError(f"cannot parse runtime version file {path}: {exc}") from exc
    values = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "VERSION" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.append(value.value)
        else:
            raise ValueError("sonder_version.VERSION must be a string literal")
    if len(values) != 1:
        raise ValueError("sonder_version.py must define VERSION exactly once")
    return values[0]


def parse_app_version(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read Flutter pubspec {path}: {exc}") from exc
    values = []
    for line in lines:
        match = re.fullmatch(r"version:\s*([^#\s]+)\s*(?:#.*)?", line)
        if match:
            values.append(match.group(1))
    if len(values) != 1:
        raise ValueError("app/pubspec.yaml must define top-level version exactly once")
    return values[0]


def git_revision(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    revision = result.stdout.strip().lower()
    return revision if result.returncode == 0 and SHA_RE.fullmatch(revision) else "unknown"


def evaluate(
    runtime_raw: str,
    app_raw: str,
    *,
    tag_raw: str | None = None,
    revision: str = "unknown",
    require_release: bool = False,
) -> dict[str, Any]:
    diagnostics: list[Diagnostic] = []
    runtime_match = RUNTIME_RE.fullmatch(runtime_raw)
    app_match = APP_RE.fullmatch(app_raw)
    tag_match = TAG_RE.fullmatch(tag_raw) if tag_raw else None

    if not runtime_match:
        diagnostics.append(Diagnostic(
            "RUNTIME_VERSION_INVALID", "error",
            "runtime VERSION must be MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH.devN",
        ))
    if not app_match:
        diagnostics.append(Diagnostic(
            "APP_VERSION_INVALID", "error",
            "Flutter version must be MAJOR.MINOR.PATCH+BUILD with BUILD >= 1",
        ))
    if tag_raw and not tag_match:
        diagnostics.append(Diagnostic(
            "TAG_INVALID", "error",
            "release tag must be exactly app-vMAJOR.MINOR.PATCH",
        ))

    runtime_core = runtime_match.group("core") if runtime_match else None
    app_core = app_match.group("core") if app_match else None
    tag_core = tag_match.group("core") if tag_match else None
    is_development = bool(runtime_match and runtime_match.group("dev") is not None)
    mode = "invalid"
    release_version = None

    if runtime_match and app_match and (not tag_raw or tag_match):
        if tag_match:
            mode = "release"
            release_version = tag_core
            if is_development:
                diagnostics.append(Diagnostic(
                    "TAGGED_RUNTIME_IS_DEVELOPMENT", "error",
                    f"tag {tag_raw} cannot publish development runtime {runtime_raw}",
                ))
            if runtime_core != tag_core:
                diagnostics.append(Diagnostic(
                    "RUNTIME_TAG_MISMATCH", "error",
                    f"runtime {runtime_core} does not match tag {tag_core}",
                ))
            if app_core != tag_core:
                diagnostics.append(Diagnostic(
                    "APP_TAG_MISMATCH", "error",
                    f"Flutter base {app_core} does not match tag {tag_core}",
                ))
        elif is_development:
            mode = "development"
            diagnostics.append(Diagnostic(
                "DEVELOPMENT_NOT_RELEASE_READY", "info",
                f"runtime {runtime_raw} is a development identity; no release tag is active",
            ))
            if runtime_core != app_core:
                diagnostics.append(Diagnostic(
                    "DEVELOPMENT_VERSION_DIVERGENCE", "warning",
                    f"runtime base {runtime_core} and Flutter base {app_core} may diverge only before release",
                ))
        else:
            mode = "release-candidate"
            release_version = runtime_core
            if runtime_core != app_core:
                diagnostics.append(Diagnostic(
                    "STABLE_VERSION_MISMATCH", "error",
                    f"stable runtime {runtime_core} must match Flutter base {app_core}",
                ))

    normalized_revision = revision.strip().lower()
    if normalized_revision != "unknown" and not SHA_RE.fullmatch(normalized_revision):
        diagnostics.append(Diagnostic(
            "REVISION_INVALID", "error",
            "build revision must be a full 40-character Git commit SHA",
        ))
    elif normalized_revision == "unknown":
        severity = "error" if tag_raw or require_release else "warning"
        diagnostics.append(Diagnostic(
            "REVISION_UNKNOWN", severity,
            "exact build identity requires a full Git commit SHA",
        ))

    if require_release and not tag_raw:
        diagnostics.append(Diagnostic(
            "RELEASE_TAG_REQUIRED", "error",
            "--require-release requires an app-vMAJOR.MINOR.PATCH tag",
        ))

    ok = not any(item.severity == "error" for item in diagnostics)
    release_ready = ok and mode == "release"
    # Never label a mismatched build with the requested tag. Until the entire
    # release contract passes, its truthful identity remains the runtime value.
    identity_version = release_version if release_ready else runtime_raw
    short_revision = normalized_revision[:12] if SHA_RE.fullmatch(normalized_revision) else "unknown"
    return {
        "schema": 1,
        "ok": ok,
        "mode": mode,
        "release_ready": release_ready,
        "release_version": release_version,
        "runtime": {"raw": runtime_raw, "base": runtime_core, "development": is_development},
        "app": {
            "raw": app_raw,
            "base": app_core,
            "build": int(app_match.group("build")) if app_match else None,
        },
        "tag": {"raw": tag_raw, "version": tag_core} if tag_raw else None,
        "build_identity": {
            "display": f"sonder-runtime/{identity_version}@{short_revision}",
            "runtime_version": runtime_raw,
            "app_version": app_raw,
            "release_version": release_version,
            "commit_sha": normalized_revision,
        },
        "diagnostics": [item.as_dict() for item in diagnostics],
    }


def check_repository(
    root: Path,
    *,
    tag: str | None = None,
    revision: str | None = None,
    require_release: bool = False,
) -> dict[str, Any]:
    root = root.resolve()
    return evaluate(
        parse_runtime_version(root / "sonder_version.py"),
        parse_app_version(root / "app/pubspec.yaml"),
        tag_raw=tag,
        revision=revision or git_revision(root),
        require_release=require_release,
    )


def _render(report: dict[str, Any]) -> str:
    lines = [
        f"version policy: {'PASS' if report['ok'] else 'FAIL'} ({report['mode']})",
        f"build identity: {report['build_identity']['display']}",
        f"runtime: {report['runtime']['raw']}",
        f"Flutter: {report['app']['raw']}",
        f"tag: {report['tag']['raw'] if report['tag'] else '(none)'}",
        f"release ready: {'yes' if report['release_ready'] else 'no'}",
    ]
    for item in report["diagnostics"]:
        lines.append(f"{item['severity'].upper()} {item['code']}: {item['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--tag", default=None, help="release tag, for example app-v1.2.3")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--require-release", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    tag = args.tag
    if tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
    try:
        report = check_repository(
            args.root, tag=tag, revision=args.revision,
            require_release=args.require_release,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
