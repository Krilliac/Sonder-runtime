"""Adversarial path-hardening tests for PR #842 (assetgen + artifact_grounding).

Every hostile lexical input (UNC, device/NT prefix, drive-qualified, rooted,
traversal, ADS/colon, control char/NUL, Windows reserved device name,
dot/space alias, non-string JSON type) must be refused BEFORE any filesystem
call (``realpath``, ``resolve``, ``exists``, ``stat``, ``lstat``).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import assetgen
import sonder_runtime.adapters.artifact_grounding as artifact_grounding

BS = chr(92)

HOSTILE_MANIFEST_ROWS = [
    # UNC and mixed-slash spellings
    BS * 2 + "nohost.invalid" + BS + "share" + BS + "x.png",
    "//nohost.invalid/share/x.png",
    BS + "/nohost.invalid/share/x.png",
    "/" + BS + "nohost.invalid/share/x.png",
    " " + BS * 2 + "nohost.invalid" + BS + "share" + BS + "x.png",
    " //nohost.invalid/share/x.png",
    # Extended, device, and NT prefixes (including leading-space variants)
    BS * 2 + "?" + BS + "UNC" + BS + "nohost.invalid" + BS + "share" + BS + "x.png",
    BS * 2 + "?" + BS + "C:" + BS + "x.png",
    "//?/C:/x.png",
    " " + BS * 2 + "?" + BS + "C:" + BS + "x.png",
    " //?/C:/x.png",
    BS * 2 + "." + BS + "pipe" + BS + "x",
    "//./pipe/x",
    BS + "??" + BS + "C:" + BS + "x.png",
    "/??/C:/x.png",
    # Drive-qualified and root-relative
    "C:x.png",
    "C:" + BS + "x.png",
    "C:/x.png",
    " C:x.png",
    "/x.png",
    BS + "x.png",
    # Traversal, dot segments, empty/whitespace, Windows trailing-dot/space aliases
    "../x.png",
    ".." + BS + "x.png",
    "a/../b.png",
    "a/.." + BS + "b.png",
    ".",
    "./x.png",
    "." + BS + "x.png",
    "a/./b.png",
    "a//b.png",
    "a/",
    "...",
    "",
    " ",
    "x.png.",
    "x.png ",
    " x.png",
    "sub./x.png",
    "sub /x.png",
    # Alternate data streams / colons
    "x.png::$DATA",
    "x.png:stream",
    ":stream",
    "sub/a:b",
    # NUL and control characters
    "x\x00.png",
    "x\n.png",
    "x\r.png",
    "x\t.png",
    "x\x01.png",
    "x\x7f.png",
    # Windows reserved DOS device names
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM9",
    "LPT1",
    "LPT9",
    "CON.png",
    "NUL.json",
    "com1.txt",
    "NUL ",
    "sub/CON.png",
    "COM\u00b9.txt",
    # Non-string JSON values
    123,
    None,
    True,
    ["x.png"],
    {"path": "x.png"},
]

NETWORK_ROOT_SPELLINGS = [
    BS * 2 + "nohost.invalid" + BS + "share" + BS + "pack",
    "//nohost.invalid/share/pack",
    BS + "/nohost.invalid/share/pack",
    "/" + BS + "nohost.invalid/share/pack",
    BS * 2 + "?" + BS + "UNC" + BS + "nohost.invalid" + BS + "share" + BS + "pack",
    BS * 2 + "." + BS + "pipe" + BS + "x",
    "//./pipe/x",
    BS + "??" + BS + "C:" + BS + "pack",
    "/??/C:/pack",
]


@pytest.mark.parametrize("raw", HOSTILE_MANIFEST_ROWS)
def test_lexical_validators_refuse_hostile_manifest_rows_with_zero_fs_calls(
    monkeypatch, tmp_path, raw
):
    touched = []

    def _forbid(name):
        def _boom(*args, **kwargs):
            touched.append((name, args))
            raise AssertionError("%s should not be called for %r: %r" % (name, raw, args))
        return _boom

    monkeypatch.setattr(os.path, "realpath", _forbid("realpath"))
    monkeypatch.setattr(os, "stat", _forbid("stat"))
    monkeypatch.setattr(os, "lstat", _forbid("lstat"))
    monkeypatch.setattr(Path, "resolve", _forbid("Path.resolve"))
    monkeypatch.setattr(Path, "exists", _forbid("Path.exists"))
    monkeypatch.setattr(Path, "lstat", _forbid("Path.lstat"))
    monkeypatch.setattr(Path, "is_symlink", _forbid("Path.is_symlink"))

    assert assetgen._manifest_row_name_is_safe(raw) is False
    assert artifact_grounding._safe_manifest_path(tmp_path, raw) is None
    assert touched == []


@pytest.mark.parametrize(
    "name,allow_subdirs",
    [
        ("icon.png", False),
        ("my image.png", False),
        (".hidden", False),
        ("console.png", False),
        ("null.json", False),
        ("aux_file.txt", False),
        ("com10.txt", False),
        ("lpt0.txt", False),
        ("sub/icon.png", True),
        ("sub" + BS + "icon.png", True),
    ],
)
def test_safe_relative_parts_accepts_legitimate_filenames(name, allow_subdirs):
    parts = artifact_grounding._safe_relative_parts(name, allow_subdirs=allow_subdirs)
    assert parts is not None
    if not allow_subdirs:
        assert assetgen._manifest_row_name_is_safe(name) is True


@pytest.mark.parametrize(
    "alias",
    ["brief.md.", "brief.md ", "./brief.md", "." + BS + "brief.md", "brief.md::$DATA"],
)
def test_manifest_alias_and_ads_rows_are_refused_by_verify_pack_and_grounding(
    monkeypatch, tmp_path, alias
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = tmp_path / "artifacts" / "generated" / "alias-pack"
    pack.mkdir(parents=True)
    data = b"# Heading\n\nBody text.\n"
    (pack / "brief.md").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (pack / "manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {"path": "brief.md", "bytes": len(data), "sha256": digest},
                    {"path": alias, "bytes": len(data), "sha256": digest},
                ],
                "kinds": [],
            }
        ),
        encoding="utf-8",
    )

    vp = assetgen.verify_pack(str(pack))
    assert vp["ok"] is False
    assert any(f.startswith("unsafe manifest path:") for f in vp["failures"]), vp["failures"]

    ag = artifact_grounding.validate(pack, "bundle", {"require_manifest": True})
    assert ag["ok"] is False
    assert any(
        c["name"] == "bundle-safe-path" and not c["ok"] for c in ag["checks"]
    ), ag["checks"]


def test_non_string_manifest_row_path_refused_even_when_stringified_file_exists(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = tmp_path / "artifacts" / "generated" / "int-pack"
    pack.mkdir(parents=True)
    data = b"plain text\n"
    (pack / "123").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (pack / "manifest.json").write_text(
        json.dumps({"files": [{"path": 123, "bytes": len(data), "sha256": digest}], "kinds": []}),
        encoding="utf-8",
    )

    vp = assetgen.verify_pack(str(pack))
    assert vp["ok"] is False
    assert any("unsafe manifest path" in f for f in vp["failures"])

    ag = artifact_grounding.validate(pack, "bundle", {"require_manifest": True})
    assert ag["ok"] is False
    assert any(c["name"] == "bundle-safe-path" and not c["ok"] for c in ag["checks"])


@pytest.mark.parametrize("spelling", NETWORK_ROOT_SPELLINGS)
def test_direct_artifact_grounding_validate_refuses_network_path_before_fs(
    monkeypatch, spelling
):
    touched = []

    def _forbid(name):
        def _boom(self, *args, **kwargs):
            touched.append((name, str(self)))
            raise AssertionError("%s saw network spelling %r" % (name, str(self)))
        return _boom

    monkeypatch.setattr(Path, "exists", _forbid("Path.exists"))
    monkeypatch.setattr(Path, "resolve", _forbid("Path.resolve"))
    monkeypatch.setattr(Path, "lstat", _forbid("Path.lstat"))

    result = artifact_grounding.validate(spelling)
    assert result["ok"] is False
    assert result["checks"][0]["name"] == "network-path"
    assert touched == []


def test_safe_manifest_path_refuses_reparse_component_before_resolve(
    monkeypatch, tmp_path
):
    pack = tmp_path / "pack"
    sub = pack / "sub"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("ok", encoding="utf-8")

    resolved_targets = []
    real_resolve = Path.resolve

    def spy_resolve(self, *args, **kwargs):
        resolved_targets.append(str(self))
        return real_resolve(self, *args, **kwargs)

    real_reparse = artifact_grounding._is_reparse_point
    monkeypatch.setattr(
        artifact_grounding,
        "_is_reparse_point",
        lambda p: p == sub or real_reparse(p),
    )
    monkeypatch.setattr(Path, "resolve", spy_resolve)

    assert artifact_grounding._safe_manifest_path(pack, "sub/a.txt") is None
    assert not any("a.txt" in item for item in resolved_targets), resolved_targets


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction test")
def test_safe_manifest_path_refuses_real_windows_directory_junction(monkeypatch, tmp_path):
    pack = tmp_path / "pack"
    real_sub = pack / "real_sub"
    real_sub.mkdir(parents=True)
    (real_sub / "a.txt").write_text("ok", encoding="utf-8")
    junc = pack / "junc_sub"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junc), str(real_sub)],
        check=True,
        capture_output=True,
    )
    try:
        resolved_targets = []
        real_resolve = Path.resolve

        def spy_resolve(self, *args, **kwargs):
            resolved_targets.append(str(self))
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", spy_resolve)
        assert artifact_grounding._is_reparse_point(junc) is True
        assert artifact_grounding._safe_manifest_path(pack.resolve(), "junc_sub/a.txt") is None
        assert not any("junc_sub" in item for item in resolved_targets), resolved_targets
    finally:
        os.rmdir(str(junc))


def test_verify_pack_and_grounding_refuse_reparse_manifest_and_row_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = tmp_path / "artifacts" / "generated" / "link-pack"
    pack.mkdir(parents=True)
    data = b"# Heading\n\nBody.\n"
    (pack / "brief.md").write_bytes(data)
    manifest_file = pack / "manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "path": "brief.md",
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                ],
                "kinds": [],
            }
        ),
        encoding="utf-8",
    )

    real_reparse = artifact_grounding._is_reparse_point
    # 1. Symlinked/reparse manifest.json is refused before reading in both entry points.
    monkeypatch.setattr(
        artifact_grounding,
        "_is_reparse_point",
        lambda p: Path(p).resolve() == manifest_file.resolve() or real_reparse(p),
    )
    with pytest.raises(ValueError, match="symlink or reparse point"):
        assetgen.verify_pack(str(pack))
    ag = artifact_grounding.validate(pack, "bundle", {"require_manifest": True})
    assert ag["ok"] is False
    assert any(
        c["name"] == "bundle-manifest-json" and not c["ok"] for c in ag["checks"]
    ), ag["checks"]

    # 2. Symlinked/reparse row target inside pack is refused before hashing.
    hashed = []
    real_hash = assetgen._hash_file
    monkeypatch.setattr(
        assetgen, "_hash_file", lambda p: hashed.append(p) or real_hash(p)
    )
    monkeypatch.setattr(
        artifact_grounding,
        "_is_reparse_point",
        lambda p: Path(p).name == "brief.md" or real_reparse(p),
    )
    vp = assetgen.verify_pack(str(pack))
    assert vp["ok"] is False
    assert any("unsafe manifest path" in f for f in vp["failures"]), vp["failures"]
    assert hashed == []


def test_html_validator_refuses_reserved_device_control_and_alias_refs_before_fs(
    monkeypatch, tmp_path
):
    page = tmp_path / "preview.html"
    page.write_text(
        "<!doctype html><html><body>"
        '<img src="CON">'
        '<img src="NUL.png">'
        '<img src="brief.md.">'
        '<img src="brief.md%20">'
        '<img src="%0aevil.png">'
        + '<img src="' + BS * 2 + "nohost.invalid" + BS + "share" + BS + 'x.png">'
        + "</body></html>",
        encoding="utf-8",
    )
    touched = []
    real_resolve = Path.resolve
    real_exists = Path.exists

    def spy_resolve(self, *args, **kwargs):
        touched.append(str(self))
        return real_resolve(self, *args, **kwargs)

    def spy_exists(self, *args, **kwargs):
        touched.append(str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    monkeypatch.setattr(Path, "exists", spy_exists)

    checks = []
    artifact_grounding._validate_html(page, {"no_external_dependencies": True}, checks)
    monkeypatch.undo()

    bad_tokens = ("CON", "NUL", "brief.md.", "brief.md ", "evil.png", "nohost.invalid")
    assert not any(any(tok in item for tok in bad_tokens) for item in touched), touched
    local = next(c for c in checks if c["name"] == "html-local-references")
    assert local["ok"] is False
    external = next(c for c in checks if c["name"] == "html-no-external-dependencies")
    assert external["ok"] is False


def test_edl_validator_refuses_ads_device_and_alias_clip_names_before_resolve(
    monkeypatch, tmp_path
):
    edl = tmp_path / "timeline.edl"
    edl.write_text(
        "TITLE: TEST\n"
        "FCM: NON-DROP FRAME\n"
        "001  AX       V     C        00:00:00:00 00:00:01:00 00:00:00:00 00:00:01:00\n"
        "* FROM CLIP NAME: sub/clip.avi:stream\n"
        "* FROM CLIP NAME: CON.avi\n"
        "* FROM CLIP NAME: clip.avi.\n"
        "* FROM CLIP NAME: " + BS * 2 + "nohost.invalid" + BS + "share" + BS + "x.avi\n",
        encoding="utf-8",
    )
    touched = []
    real_resolve = Path.resolve

    def spy_resolve(self, *args, **kwargs):
        touched.append(str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    checks = []
    artifact_grounding._validate_edl(edl, {"no_external_dependencies": True}, checks)
    monkeypatch.undo()

    bad_tokens = ("stream", "CON.avi", "clip.avi.", "nohost.invalid")
    assert not any(any(tok in item for tok in bad_tokens) for item in touched), touched
    safe = next(c for c in checks if c["name"] == "edl-safe-media-references")
    assert safe["ok"] is False


def test_resolve_pack_dir_uses_stripped_extended_local_path(monkeypatch, tmp_path):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    assets_root = tmp_path / "artifacts" / "generated"
    assets_root.mkdir(parents=True)
    extended = BS * 2 + "?" + BS + str(assets_root.resolve())

    resolved = assetgen.resolve_pack_dir("demo-pack", extended)
    assert not resolved.startswith(BS * 2 + "?" + BS)
    assert Path(resolved) == (assets_root / "demo-pack").resolve()
