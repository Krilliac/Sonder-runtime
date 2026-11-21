"""Policy-root parsing for ``SONDER_FILE_ROOTS`` and ``file_roots.local``.

Extended-length local paths (``\\\\?\\C:\\...`` and ``//?/C:/...``) must have
their prefix stripped with ``_EXTENDED_LOCAL_RE`` before ``Path()`` is built so
containment checks match ``resolve_path`` across Python versions (Python 3.12's
``Path.resolve()`` preserves ``//?/`` while ``_reject_network_path`` strips it).
Every other prefixed/network/device form must be dropped and never become an
allowed root or reach ``Path.resolve()``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.filesystem import file_ops

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows drive/extended path semantics")

DISALLOWED_ROOT_FORMS = [
    pytest.param("\\\\?\\UNC\\server\\share\\dir", id="extended-unc-backslash"),
    pytest.param("//?/UNC/server/share/dir", id="extended-unc-slash"),
    pytest.param("\\\\?\\unc\\server\\share", id="extended-unc-lowercase"),
    pytest.param("\\\\.\\C:\\dir", id="device-drive-backslash"),
    pytest.param("\\\\.\\PhysicalDrive0", id="device-physical-drive"),
    pytest.param("\\\\.\\pipe\\sonder", id="device-named-pipe"),
    pytest.param("//./C:/dir", id="device-drive-slash"),
    pytest.param("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\dir", id="extended-globalroot-backslash"),
    pytest.param("//?/GLOBALROOT/Device/HarddiskVolume1/dir", id="extended-globalroot-slash"),
    pytest.param("\\\\.\\GLOBALROOT\\Device\\HarddiskVolume1\\dir", id="device-globalroot"),
    pytest.param("\\??\\GLOBALROOT\\Device\\HarddiskVolume1\\dir", id="nt-globalroot-backslash"),
    pytest.param("/??/GLOBALROOT/Device/HarddiskVolume1/dir", id="nt-globalroot-slash"),
    pytest.param("\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\dir", id="extended-volume-backslash"),
    pytest.param("//?/Volume{01234567-89ab-cdef-0123-456789abcdef}/dir", id="extended-volume-slash"),
    pytest.param("\\\\.\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\dir", id="device-volume"),
    pytest.param("\\??\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\dir", id="nt-volume"),
    pytest.param("\\\\host\\share", id="plain-unc-root"),
    pytest.param("\\\\host\\share\\dir", id="plain-unc-subdir"),
    pytest.param("//host/share", id="plain-unc-slash-root"),
    pytest.param("//host/share/dir", id="plain-unc-slash-subdir"),
    pytest.param("\\\\localhost\\C$\\dir", id="admin-share-unc"),
    pytest.param("\\/host\\share", id="mixed-leading-slashes-unc-1"),
    pytest.param("/\\host/share", id="mixed-leading-slashes-unc-2"),
    pytest.param("\\??\\C:\\dir", id="nt-namespace-drive-backslash"),
    pytest.param("/??/C:/dir", id="nt-namespace-drive-slash"),
    pytest.param("\\??", id="nt-namespace-bare"),
    pytest.param("\\\\?\\C:", id="extended-drive-without-separator"),
    pytest.param("\\\\?/C:\\dir", id="extended-mixed-prefix-1"),
    pytest.param("/\\?\\C:\\dir", id="extended-mixed-prefix-2"),
    pytest.param("\\\\?\\\\C:\\dir", id="extended-double-separator"),
    pytest.param("\\\\\\?\\C:\\dir", id="extended-triple-leading-backslash"),
]


@pytest.fixture
def isolated_root_policy(monkeypatch, tmp_path):
    """Isolate env and roots-file lookups from the host machine."""
    roots_file = tmp_path / "file_roots.local"
    monkeypatch.setenv("SONDER_FILE_ROOTS", "")
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(roots_file))
    return roots_file


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("\\\\?\\C:\\work\\proj", Path("C:\\work\\proj")),
        ("//?/C:/work/proj", Path("C:/work/proj")),
        ("\\\\?\\D:/work/proj", Path("D:/work/proj")),
        ("//?/D:\\work\\proj", Path("D:\\work\\proj")),
        ("\\\\?\\c:\\lower\\proj", Path("c:\\lower\\proj")),
        ("\\\\?\\C:\\", Path("C:\\")),
    ],
    ids=[
        "backslash-prefix",
        "slash-prefix",
        "backslash-prefix-slash-body",
        "slash-prefix-backslash-body",
        "lowercase-drive",
        "drive-root",
    ],
)
def test_extended_local_prefix_is_stripped_before_path_construction(isolated_root_policy, raw, expected):
    """Strip ``\\\\?\\`` / ``//?/`` before ``Path()`` so behaviour does not depend on Python's ``resolve()``."""
    isolated_root_policy.write_text(raw + "\n", encoding="utf-8")
    from_file = file_ops._roots_from_file()
    assert from_file == [expected]
    assert not str(from_file[0]).startswith(("\\\\?\\", "//?/"))

    if os.pathsep != ":":
        from_env = file_ops._split_roots(raw)
        assert from_env == [expected]
        assert not str(from_env[0]).startswith(("\\\\?\\", "//?/"))


@pytest.mark.parametrize("raw", DISALLOWED_ROOT_FORMS)
def test_disallowed_prefixed_root_is_dropped_from_roots_file(isolated_root_policy, raw):
    isolated_root_policy.write_text(raw + "\n", encoding="utf-8")
    assert file_ops._roots_from_file() == []


@pytest.mark.parametrize("raw", DISALLOWED_ROOT_FORMS)
def test_disallowed_prefixed_root_is_dropped_from_split_roots(raw):
    if os.pathsep == ":" and ":" in raw:
        pytest.skip("Windows drive colon collides with POSIX os.pathsep")
    assert file_ops._split_roots(raw) == []


def test_nul_byte_root_entry_is_dropped(isolated_root_policy):
    bad = "C:\\work\\evil\x00suffix"
    isolated_root_policy.write_text(bad + "\n", encoding="utf-8")
    assert file_ops._roots_from_file() == []
    if os.pathsep != ":":
        assert file_ops._split_roots(bad) == []


@pytest.mark.parametrize("raw", DISALLOWED_ROOT_FORMS)
def test_disallowed_root_never_enters_allowed_roots_or_calls_resolve(isolated_root_policy, monkeypatch, raw):
    baseline = file_ops.allowed_roots()
    seen_resolve_inputs: list[str] = []
    real_resolve = Path.resolve

    def spy_resolve(self, *args, **kwargs):
        text = str(self)
        seen_resolve_inputs.append(text)
        flat = text.replace("/", "\\")
        if flat.startswith(("\\\\", "\\??\\")) or flat == "\\??":
            raise AssertionError("Path.resolve() must never be called on disallowed root: %r" % text)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)

    isolated_root_policy.write_text(raw + "\n", encoding="utf-8")
    assert file_ops.allowed_roots() == baseline

    if os.pathsep != ":" or ":" not in raw:
        isolated_root_policy.unlink()
        monkeypatch.setenv("SONDER_FILE_ROOTS", raw)
        assert file_ops.allowed_roots() == baseline
        assert file_ops.allowed_roots(extra_roots=raw) == baseline

    raw_flat = raw.replace("/", "\\").lower()
    for seen in seen_resolve_inputs:
        seen_flat = seen.replace("/", "\\").lower()
        assert not seen_flat.startswith(("\\\\", "\\??\\")), seen
        assert raw_flat not in seen_flat, seen


@WINDOWS_ONLY
@pytest.mark.parametrize("prefix", ["\\\\?\\", "//?/"], ids=["backslash", "slash"])
def test_extended_local_root_in_env_allows_plain_and_extended_caller_paths(
    isolated_root_policy, monkeypatch, tmp_path, prefix,
):
    project = tmp_path / "proj"
    project.mkdir()
    note = project / "note.txt"
    note.write_text("ok\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_note = outside / "secret.txt"
    outside_note.write_text("no\n", encoding="utf-8")

    spelled_root = prefix + (project.as_posix() if prefix == "//?/" else str(project))
    monkeypatch.setenv("SONDER_FILE_ROOTS", spelled_root)

    roots = file_ops.allowed_roots()
    assert project.resolve() in roots
    assert all(not str(root).startswith(("\\\\?\\", "//?/")) for root in roots)

    assert file_ops.resolve_path(str(note)) == note.resolve()
    assert file_ops.resolve_path("\\\\?\\" + str(note)) == note.resolve()
    assert file_ops.resolve_path("//?/" + note.as_posix()) == note.resolve()

    grants = file_ops.lane_grant_roots()
    assert str(project.resolve()) in grants
    assert all(not grant.startswith(("\\\\?\\", "//?/")) for grant in grants)

    with pytest.raises(PermissionError, match="outside allowed roots"):
        file_ops.resolve_path(str(outside_note))


@WINDOWS_ONLY
def test_extended_local_root_in_roots_file_allows_caller_paths(isolated_root_policy, tmp_path):
    project = tmp_path / "from_file_proj"
    project.mkdir()
    note = project / "note.txt"
    note.write_text("ok\n", encoding="utf-8")

    isolated_root_policy.write_text(
        "\n".join([
            "# comment line",
            "\\\\?\\UNC\\server\\share\\bad",
            "\\\\.\\PhysicalDrive0",
            "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\bad",
            "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\bad",
            "\\\\host\\share\\bad",
            "  \\\\?\\" + str(project) + "  ",
        ]) + "\n",
        encoding="utf-8",
    )

    assert file_ops._roots_from_file() == [Path(str(project))]
    roots = file_ops.allowed_roots()
    assert project.resolve() in roots
    assert all(not str(root).startswith(("\\\\", "//", "\\??")) for root in roots)
    assert file_ops.resolve_path(str(note)) == note.resolve()
    assert file_ops.resolve_path("\\\\?\\" + str(note)) == note.resolve()
    assert str(project.resolve()) in file_ops.lane_grant_roots()


@WINDOWS_ONLY
def test_mixed_valid_and_disallowed_env_roots_keep_only_valid(isolated_root_policy, monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    combined = os.pathsep.join([
        "\\\\host\\share",
        "\\\\?\\UNC\\server\\share",
        "\\\\.\\C:\\device",
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\x",
        "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\x",
        "\\\\?\\" + str(first),
        str(second),
    ])
    monkeypatch.setenv("SONDER_FILE_ROOTS", combined)

    parsed = file_ops._split_roots(combined)
    assert parsed == [Path(str(first)), Path(str(second))]

    roots = file_ops.allowed_roots()
    assert first.resolve() in roots
    assert second.resolve() in roots
    assert all(not str(root).startswith(("\\\\", "//", "\\??")) for root in roots)


@WINDOWS_ONLY
def test_reach_scope_strips_extended_local_and_drops_disallowed(isolated_root_policy, tmp_path):
    project = tmp_path / "reach_proj"
    project.mkdir()
    note = project / "note.txt"
    note.write_text("ok\n", encoding="utf-8")

    with file_ops.reach_scope(lambda: "\\\\host\\share" + os.pathsep + "\\\\?\\" + str(project)):
        assert file_ops._reach_roots() == [Path(str(project))]
        roots = file_ops.allowed_roots()
        assert project.resolve() in roots
        assert all(not str(root).startswith(("\\\\", "//", "\\??")) for root in roots)
        assert file_ops.resolve_path(str(note)) == note.resolve()
