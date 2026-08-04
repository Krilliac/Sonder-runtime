"""Single authoritative version and build identity for the Sonder runtime.

Every surface that reports a version — ``/version``, diagnostics, backup
manifests, migration ledgers, release directories — must import from here
so a release has exactly one identity.  Release tooling stamps
``sonder_build.json`` next to this module when building an immutable
release directory; a source checkout falls back to asking git.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Bumped by the release process.  The ".dev0" suffix marks a build taken
# from a mutable source checkout rather than a stamped release artifact.
VERSION = "0.9.0.dev0"

_BUILD_STAMP = Path(__file__).resolve().with_name("sonder_build.json")


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit_sha: str
    stamped: bool

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "commit_sha": self.commit_sha,
            "stamped": self.stamped,
        }


def _commit_from_git() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else "unknown"


def build_info() -> BuildInfo:
    if _BUILD_STAMP.exists():
        try:
            raw = json.loads(_BUILD_STAMP.read_text(encoding="utf-8"))
            version = str(raw.get("version") or VERSION)
            commit = str(raw.get("commit_sha") or "unknown")
            return BuildInfo(version=version, commit_sha=commit, stamped=True)
        except (OSError, ValueError):
            pass
    return BuildInfo(version=VERSION, commit_sha=_commit_from_git(), stamped=False)
