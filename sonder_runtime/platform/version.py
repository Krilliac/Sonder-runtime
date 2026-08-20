"""Canonical build identity implementation for the Sonder runtime.

The repository-root :mod:`sonder_version` module remains a deliberately tiny
compatibility surface because release tooling parses its literal ``VERSION``
assignment without importing Python.  Runtime code must import this module so
the implementation and build-stamp lookup live inside the packaged runtime.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Keep this literal synchronized with the release-tooling source contract in
# ``sonder_version.py``.  The release policy tests assert that both surfaces
# expose the same version; the root file remains the source parsed by tooling.
VERSION = "0.9.0.dev0"

# The build stamp is emitted at the package root by package_local_system.py.
_BUILD_STAMP = Path(__file__).resolve().parents[2] / "sonder_build.json"


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
            cwd=Path(__file__).resolve().parents[2],
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
