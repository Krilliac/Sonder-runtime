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
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.bootstrap import app as runtime_app
from sonder_runtime.interfaces.http import serve as http_serve
from sonder_runtime.platform import config as runtime_config

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
    ],
    ids=[
        "backslash-prefix",
        "slash-prefix",
        "backslash-prefix-slash-body",
        "slash-prefix-backslash-body",
        "lowercase-drive",
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


DRIVE_AND_RELATIVE_ROOT_FORMS = [
    pytest.param("C:\\", id="drive-root-backslash"),
    pytest.param("C:/", id="drive-root-slash"),
    pytest.param("d:\\", id="other-drive-root"),
    pytest.param("/", id="filesystem-root"),
    pytest.param("\\", id="filesystem-root-backslash"),
    pytest.param("C:", id="drive-relative-bare"),
    pytest.param("C:x", id="drive-relative-name"),
    pytest.param("C:x\\y", id="drive-relative-nested"),
    pytest.param("\\x", id="rooted-no-drive"),
    pytest.param("relative\\dir", id="relative"),
    pytest.param(".", id="current-dir"),
    pytest.param("..", id="parent-dir"),
    pytest.param("%SONDER_UNSET_ROOT_VAR%", id="unexpanded-percent-var"),
    pytest.param("%SONDER_UNSET_ROOT_VAR%\\proj", id="unexpanded-percent-var-nested"),
    pytest.param("$SONDER_UNSET_ROOT_VAR", id="unexpanded-dollar-var"),
    pytest.param("${SONDER_UNSET_ROOT_VAR}/proj", id="unexpanded-dollar-brace-var"),
    pytest.param("\\\\?\\C:\\", id="extended-drive-root"),
    pytest.param("//?/C:/", id="extended-drive-root-slash"),
    pytest.param("\\\\?\\C:\\x\\..\\..", id="extended-dotdot-above-start"),
    pytest.param("\\\\?\\C:\\x\\..", id="extended-dotdot-to-drive-root"),
    pytest.param("C:\\x\\..", id="dotdot-to-drive-root"),
    pytest.param("C:\\x\\y\\..\\..\\..", id="dotdot-above-drive-root"),
    pytest.param("C:\\.", id="dot-drive-root"),
]


@WINDOWS_ONLY
@pytest.mark.parametrize("raw", DRIVE_AND_RELATIVE_ROOT_FORMS)
def test_drive_root_and_relative_root_are_refused_by_every_parser(isolated_root_policy, monkeypatch, raw):
    from sonder_runtime.platform import policy_roots

    monkeypatch.delenv("SONDER_UNSET_ROOT_VAR", raising=False)
    assert file_ops._parse_policy_root(raw) is None
    with pytest.raises(policy_roots.PolicyRootError):
        policy_roots.parse_policy_root(raw)
    assert file_ops._split_roots(raw) == []

    isolated_root_policy.write_text(raw + "\n", encoding="utf-8")
    assert file_ops._roots_from_file() == []
    isolated_root_policy.unlink()

    baseline_allowed = file_ops.allowed_roots()
    baseline_grants = file_ops.lane_grant_roots(())
    assert file_ops.allowed_roots(extra_roots=raw) == baseline_allowed
    assert file_ops.lane_grant_roots((raw,)) == baseline_grants
    monkeypatch.setenv("SONDER_FILE_ROOTS", raw)
    assert file_ops.allowed_roots() == baseline_allowed
    assert file_ops.lane_grant_roots(()) == baseline_grants


def test_posix_filesystem_root_and_relative_root_are_refused():
    for raw in ("/", "//", "/.", "/x/..", "relative/dir", ".", "$SONDER_UNSET_ROOT_VAR"):
        assert file_ops._parse_policy_root(raw) is None, raw


@WINDOWS_ONLY
@pytest.mark.parametrize("home", ["C:\\", "C:", "relative-home"], ids=["drive-root", "drive-relative", "relative"])
def test_tilde_expanding_to_drive_root_or_relative_home_is_refused(monkeypatch, home):
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("HOME", home)
    assert file_ops._parse_policy_root("~") is None
    assert file_ops._split_roots("~") == []


@WINDOWS_ONLY
def test_dotdot_inside_a_root_is_normalised_and_kept_below_its_start(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    spelled = "\\\\?\\" + str(project) + "\\sub\\..\\.."
    parsed = file_ops._parse_policy_root(spelled)
    assert parsed == Path(str(tmp_path))
    assert ".." not in parsed.parts


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


def _guard_resolve_against_disallowed(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_resolve = Path.resolve

    def spy_resolve(self, *args, **kwargs):
        text = str(self)
        seen.append(text)
        flat = text.replace("/", "\\")
        if flat.startswith(("\\\\", "\\??\\")) or flat == "\\??":
            raise AssertionError("Path.resolve() must never be called on disallowed root: %r" % text)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    return seen


@pytest.mark.parametrize(
    "bad_home",
    [
        pytest.param("\\\\host\\share", id="unc-home"),
        pytest.param("//host/share", id="unc-slash-home"),
        pytest.param("\\\\?\\UNC\\nohost.invalid\\share", id="extended-unc-home"),
        pytest.param("\\\\.\\C:\\Users\\me", id="device-home"),
        pytest.param("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\Users\\me", id="globalroot-home"),
        pytest.param("\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\Users\\me", id="volume-home"),
        pytest.param("\\??\\C:\\Users\\me", id="nt-namespace-home"),
    ],
)
@pytest.mark.parametrize("tilde_entry", ["~", "~\\proj", "~/proj"], ids=["bare-tilde", "tilde-backslash", "tilde-slash"])
def test_tilde_expansion_against_disallowed_userprofile_is_dropped(
    isolated_root_policy, monkeypatch, bad_home, tilde_entry,
):
    """A UNC/prefixed ``USERPROFILE`` or ``HOME`` must not turn ``~`` into a network or device root."""
    monkeypatch.setenv("USERPROFILE", bad_home)
    monkeypatch.setenv("HOME", bad_home)
    _guard_resolve_against_disallowed(monkeypatch)

    assert file_ops._parse_policy_root(tilde_entry) is None
    assert file_ops._split_roots(tilde_entry) == []

    isolated_root_policy.write_text(tilde_entry + "\n", encoding="utf-8")
    assert file_ops._roots_from_file() == []

    baseline = file_ops.allowed_roots()
    monkeypatch.setenv("SONDER_FILE_ROOTS", tilde_entry)
    assert file_ops.allowed_roots() == baseline


@WINDOWS_ONLY
def test_tilde_expansion_against_extended_local_userprofile_strips_prefix(
    isolated_root_policy, monkeypatch, tmp_path,
):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("USERPROFILE", "\\\\?\\" + str(tmp_path))
    monkeypatch.setenv("HOME", "\\\\?\\" + str(tmp_path))

    parsed = file_ops._parse_policy_root("~\\proj")
    assert parsed == Path(str(proj))
    assert not str(parsed).startswith(("\\\\?\\", "//?/"))


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "configured_bad",
    [
        pytest.param("\\\\?\\UNC\\nohost.invalid\\share", id="extended-unc"),
        pytest.param("\\\\host\\share\\proj", id="plain-unc"),
        pytest.param("//host/share/proj", id="plain-unc-slash"),
        pytest.param("\\\\.\\C:\\proj", id="device"),
        pytest.param("\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\proj", id="globalroot"),
        pytest.param("\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\proj", id="volume"),
        pytest.param("\\??\\C:\\proj", id="nt-namespace"),
        pytest.param("/??/C:/proj", id="nt-namespace-slash"),
    ],
)
def test_lane_grant_roots_configured_drops_disallowed_and_strips_extended_local(
    isolated_root_policy, monkeypatch, tmp_path, configured_bad,
):
    plain = tmp_path / "plain"
    plain.mkdir()
    baseline = file_ops.lane_grant_roots(())
    _guard_resolve_against_disallowed(monkeypatch)

    assert file_ops.lane_grant_roots((configured_bad,)) == baseline

    grants = file_ops.lane_grant_roots((configured_bad, "\\\\?\\" + str(plain), "//?/" + plain.as_posix()))
    assert str(plain.resolve()) in grants
    assert all(not grant.startswith(("\\\\", "//", "\\??")) for grant in grants)


@WINDOWS_ONLY
def test_lane_grant_roots_configured_drops_tilde_when_userprofile_is_unc(
    isolated_root_policy, monkeypatch,
):
    monkeypatch.setenv("USERPROFILE", "\\\\nohost.invalid\\share")
    monkeypatch.setenv("HOME", "\\\\nohost.invalid\\share")
    baseline = file_ops.lane_grant_roots(())
    _guard_resolve_against_disallowed(monkeypatch)

    assert file_ops.lane_grant_roots(("~", "~\\proj")) == baseline


@WINDOWS_ONLY
def test_config_load_and_production_lane_grant_call_shape(
    isolated_root_policy, monkeypatch, tmp_path,
):
    plain = tmp_path / "plain"
    plain.mkdir()
    _guard_resolve_against_disallowed(monkeypatch)

    # Every disallowed SONDER_FILE_ROOTS entry is a config error; none is dropped silently.
    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(
            None,
            env={"SONDER_FILE_ROOTS": "\\\\?\\UNC\\nohost.invalid\\share" + os.pathsep + "\\\\host\\share" + os.pathsep + "\\??\\C:\\x"},
        )
    assert len([e for e in caught.value.errors if "workspace_roots" in e]) == 3, caught.value.errors

    # A disallowed entry next to a good one still fails the load.
    combined = os.pathsep.join([
        "\\\\?\\UNC\\nohost.invalid\\share",
        "\\\\.\\PhysicalDrive0",
        "\\\\?\\" + str(plain),
    ])
    with pytest.raises(runtime_config.ConfigError) as caught_mixed:
        runtime_config.load_config(None, env={"SONDER_FILE_ROOTS": combined})
    assert len([e for e in caught_mixed.value.errors if "workspace_roots" in e]) == 2, caught_mixed.value.errors

    # Extended-length local SONDER_FILE_ROOTS is stripped in config AND through lane_grant_roots.
    spelled = "\\\\?\\" + str(plain)
    cfg_ok = runtime_config.load_config(None, env={"SONDER_FILE_ROOTS": spelled})
    assert cfg_ok.state.workspace_roots == (str(plain),)
    assert all(not r.startswith(("\\\\", "//", "\\??")) for r in cfg_ok.state.workspace_roots)

    monkeypatch.setenv("SONDER_FILE_ROOTS", combined)
    grants = file_ops.lane_grant_roots(cfg_ok.state.workspace_roots)
    assert str(plain.resolve()) in grants
    assert all(not grant.startswith(("\\\\", "//", "\\??")) for grant in grants)


@WINDOWS_ONLY
def test_http_workspace_roots_strips_extended_local_and_drops_disallowed_and_unc_tilde(
    isolated_root_policy, monkeypatch, tmp_path,
):
    plain = tmp_path / "http_plain"
    plain.mkdir()
    monkeypatch.setenv("USERPROFILE", "\\\\nohost.invalid\\share")
    monkeypatch.setenv("HOME", "\\\\nohost.invalid\\share")
    monkeypatch.setattr(http_serve, "_admin_authorized", lambda auth: bool(auth))
    _guard_resolve_against_disallowed(monkeypatch)

    fake_app = SimpleNamespace(
        config=SimpleNamespace(
            state=SimpleNamespace(
                workspace_roots=(
                    "\\\\?\\" + str(plain),
                    "//?/" + plain.as_posix(),
                    "\\\\?\\UNC\\nohost.invalid\\share",
                    "\\\\host\\share\\proj",
                    "\\\\.\\C:\\proj",
                    "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\proj",
                    "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\proj",
                    "\\??\\C:\\proj",
                    "~",
                    "~\\proj",
                ),
            ),
        ),
    )
    monkeypatch.setattr(runtime_app, "default_app", lambda: fake_app)

    assert http_serve._http_workspace_roots(False) == ()
    roots = http_serve._http_workspace_roots(True)
    assert roots == (str(plain.resolve()),)
    assert all(not root.startswith(("\\\\", "//", "\\??")) for root in roots)


WINDOWS_ALIAS_BYPASS_FORMS = [
    pytest.param("C:\\...", id="triple-dot"),
    pytest.param("C:\\....", id="quad-dot"),
    pytest.param("C:\\ .", id="space-dot"),
    pytest.param("C:\\ \\", id="space-component"),
    pytest.param("C:\\. \\", id="dot-space-component"),
    pytest.param("C:\\...\\", id="triple-dot-trailing-sep"),
    pytest.param("C:\\a\\..\\...", id="dotdot-to-triple-dot"),
    pytest.param("C:\\a\\..\\ \\", id="dotdot-to-space-component"),
    pytest.param("\\\\?\\C:\\...", id="extended-triple-dot"),
    pytest.param("\\\\?\\C:\\ \\", id="extended-space-component"),
    pytest.param("//?/C:/...", id="extended-slash-triple-dot"),
    pytest.param("//?/C:/ /", id="extended-slash-space-component"),
    pytest.param("C:\\a:b", id="ads-colon"),
    pytest.param("C:\\::$DATA", id="ads-root-data"),
    pytest.param("C:\\.:$DATA", id="ads-dot-data"),
    pytest.param("C:\\foo::$DATA", id="ads-file-data"),
    pytest.param("C:\\foo.", id="trailing-dot-component"),
    pytest.param("C:\\foo \\bar", id="trailing-space-parent-component"),
    pytest.param("1:\\foo", id="digit-drive"),
    pytest.param("@:\\foo", id="symbol-drive"),
]


@WINDOWS_ONLY
@pytest.mark.parametrize("raw", WINDOWS_ALIAS_BYPASS_FORMS)
def test_windows_alias_ads_and_nonletter_drive_roots_refused_everywhere(
    isolated_root_policy, monkeypatch, raw,
):
    from sonder_runtime.platform import policy_roots

    assert policy_roots.try_parse_policy_root(raw) is None
    with pytest.raises(policy_roots.PolicyRootError):
        policy_roots.parse_policy_root(raw)
    assert file_ops._split_roots(raw) == []

    isolated_root_policy.write_text(raw + "\n", encoding="utf-8")
    assert file_ops._roots_from_file() == []
    isolated_root_policy.unlink()

    baseline_allowed = file_ops.allowed_roots()
    baseline_grants = file_ops.lane_grant_roots(())
    monkeypatch.setenv("SONDER_FILE_ROOTS", raw)
    assert file_ops.allowed_roots() == baseline_allowed
    assert file_ops.lane_grant_roots(()) == baseline_grants
    assert Path("C:\\") not in file_ops.allowed_roots()
    assert "C:\\" not in file_ops.lane_grant_roots(())


@WINDOWS_ONLY
def test_triple_dot_env_root_cannot_grant_windows_system_file(isolated_root_policy, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", "C:\\...")
    with pytest.raises(PermissionError, match="outside allowed roots"):
        file_ops.resolve_path("C:\\Windows\\System32\\drivers\\etc\\hosts")


@pytest.mark.parametrize(
    "bad_value",
    [None, 123, ["C:\\foo"], Path("C:\\foo"), True, b"C:\\foo"],
    ids=["none", "int", "list", "path", "bool", "bytes"],
)
def test_non_string_policy_root_returns_none_and_raises_policy_root_error(bad_value):
    from sonder_runtime.platform import policy_roots

    assert policy_roots.try_parse_policy_root(bad_value) is None
    with pytest.raises(policy_roots.PolicyRootError):
        policy_roots.parse_policy_root(bad_value)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("C:\\foo\nbar", id="newline"),
        pytest.param("C:\\foo\rbar", id="carriage-return"),
        pytest.param("C:\\foo\x01bar", id="control-soh"),
        pytest.param("C:\\foo\x7fbar", id="control-del"),
    ],
)
def test_control_character_policy_root_is_refused(raw):
    from sonder_runtime.platform import policy_roots

    assert policy_roots.try_parse_policy_root(raw) is None
    with pytest.raises(policy_roots.PolicyRootError):
        policy_roots.parse_policy_root(raw)


def test_http_workspace_roots_does_not_expand_tilde_and_drops_aliases_and_non_strings(
    isolated_root_policy, monkeypatch, tmp_path,
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(http_serve, "_admin_authorized", lambda auth: bool(auth))

    fake_app = SimpleNamespace(
        config=SimpleNamespace(
            state=SimpleNamespace(
                workspace_roots=(
                    "~",
                    "~/proj",
                    "~\\proj",
                    "C:\\...",
                    "C:\\ \\",
                    123,
                    None,
                ),
            ),
        ),
    )
    monkeypatch.setattr(runtime_app, "default_app", lambda: fake_app)

    assert http_serve._http_workspace_roots(True) == ()


@WINDOWS_ONLY
def test_http_workspace_roots_drops_root_whose_resolution_becomes_drive_or_unc(
    isolated_root_policy, monkeypatch, tmp_path,
):
    link_like = tmp_path / "linkproj"
    link_like.mkdir()
    monkeypatch.setattr(http_serve, "_admin_authorized", lambda auth: bool(auth))
    fake_app = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(str(link_like),))),
    )
    monkeypatch.setattr(runtime_app, "default_app", lambda: fake_app)

    for resolved_target in (Path("C:\\"), Path("\\\\nohost.invalid\\share\\proj")):
        monkeypatch.setattr(Path, "resolve", lambda self, _t=resolved_target: _t)
        assert http_serve._http_workspace_roots(True) == ()
