"""A manifest row's filename must be refused lexically before any path call.

Joining a UNC or drive-qualified row onto the pack root yields that path
itself; resolving it would make Windows authenticate to the remote host.
"""

import json
import os

import pytest

import assetgen

BS = chr(92)
UNC_ROW = BS * 2 + "nohost.invalid" + BS + "share" + BS + "x.png"
UNC_SLASH_ROW = "//nohost.invalid/share/x.png"
DRIVE_ROW = "C:" + BS + "Windows" + BS + "x.png"
DRIVE_RELATIVE_ROW = "C:x.png"
EXTENDED_ROW = BS * 2 + "?" + BS + "C:" + BS + "Windows" + BS + "x.png"
DEVICE_ROW = BS * 2 + "." + BS + "C:" + BS + "x.png"
NT_ROW = BS + "??" + BS + "C:" + BS + "x.png"
ROOTED_ROW = BS + "Windows" + BS + "x.png"
DOTDOT_ROW = ".." + BS + "x.png"


def _starts_with_two_separators(value):
    text = os.fsdecode(value) if isinstance(value, (str, bytes, os.PathLike)) else ""
    return text[:2] in (BS + BS, "//", BS + "/", "/" + BS)


@pytest.mark.parametrize(
    "row",
    [UNC_ROW, UNC_SLASH_ROW, DRIVE_ROW, DRIVE_RELATIVE_ROW, EXTENDED_ROW,
     DEVICE_ROW, NT_ROW, ROOTED_ROW, DOTDOT_ROW],
)
def test_verify_pack_refuses_hostile_manifest_row_before_resolving(monkeypatch, tmp_path, row):
    monkeypatch.setattr(assetgen, "workspace_root", lambda: str(tmp_path))
    pack = tmp_path / "artifacts" / "generated" / "evil"
    pack.mkdir(parents=True)
    (pack / "manifest.json").write_text(
        json.dumps({"files": [{"path": row, "bytes": 1, "sha256": "0"}]}),
        encoding="utf-8",
    )

    seen = []
    real_realpath = os.path.realpath
    real_stat = os.stat

    def guarded_realpath(path, *args, **kwargs):
        if _starts_with_two_separators(path):
            seen.append(("realpath", path))
            raise AssertionError("realpath saw a network spelling: %r" % (path,))
        return real_realpath(path, *args, **kwargs)

    def guarded_stat(path, *args, **kwargs):
        if _starts_with_two_separators(path):
            seen.append(("stat", path))
            raise AssertionError("stat saw a network spelling: %r" % (path,))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", guarded_realpath)
    monkeypatch.setattr(os, "stat", guarded_stat)

    result = assetgen.verify_pack(str(pack))

    assert seen == []
    assert result["ok"] is False
    assert any(
        failure.startswith("unsafe manifest path") for failure in result["failures"]
    ), result["failures"]
