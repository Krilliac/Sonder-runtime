"""Build a tiny web-app bundle (root/current.json + releases/<name>) for tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULT_FILES = {
    "index.html": b"<!doctype html><title>Sonder</title><script src=\"flutter_bootstrap.js\" async></script>",
    "flutter_bootstrap.js": b"// boot",
    "main.dart.js": b"// app",
    "canvaskit/canvaskit.wasm": b"\x00asm\x01\x00\x00\x00",
    "assets/NOTICES": b"licences",
    "assets/fonts/a.ttf": b"ttf",
    "assets/font.woff2": b"woff2",
    "assets/AssetManifest.bin": b"\x00",
    "version.json": b"{}",
}


def manifest_for(files: dict[str, bytes], commit: str = "abc1234") -> dict:
    return {
        "schema": 1, "commit": commit, "dirty": False, "flutter": "test",
        "built_at": "2026-01-01T00:00:00+00:00",
        "files": {
            rel: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for rel, data in files.items()
        },
    }


def write_release(root: Path, name: str = "r1", files: dict[str, bytes] | None = None,
                  *, point: bool = True) -> Path:
    files = dict(DEFAULT_FILES if files is None else files)
    release = root / "releases" / name
    for rel, data in files.items():
        target = release / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (release / "sonder-web-manifest.json").write_text(
        json.dumps(manifest_for(files)), encoding="utf-8")
    if point:
        point_to(root, name)
    return release


def point_to(root: Path, name: str) -> None:
    tmp = root / "current.json.tmp"
    tmp.write_text(json.dumps({"release": name}), encoding="utf-8")
    os.replace(tmp, root / "current.json")
