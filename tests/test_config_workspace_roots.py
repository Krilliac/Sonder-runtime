"""``[state].workspace_roots`` is parsed once at config load and fails closed.

Every entry -- from the TOML file, from ``SONDER_FILE_ROOTS`` and from a
command-line override -- goes through the shared policy-root parser after the
sources are merged and before ``_validate`` resolves anything. A refused entry
is a configuration error, never a silent drop and never a fallback to another
source, and a refused UNC / device / NT-namespace form must not reach
``Path.resolve`` or ``os.stat`` (either can open an SMB connection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sonder_runtime.platform import config as runtime_config

BS = chr(92)
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows drive/extended path semantics")


def _bs(text: str) -> str:
    """Spell *text* with backslashes: write ``|`` for each backslash."""
    return text.replace("|", BS)


REFUSED_PREFIXED_FORMS = [
    pytest.param(_bs("||host|share|proj"), id="plain-unc"),
    pytest.param("//host/share/proj", id="plain-unc-slash"),
    pytest.param(_bs("||?|UNC|h|s|p"), id="extended-unc"),
    pytest.param(_bs("||.|C:|x"), id="device-drive"),
    pytest.param(_bs("|??|C:|x"), id="nt-namespace"),
    pytest.param(_bs("||?|GLOBALROOT|Device|HarddiskVolume1|x"), id="globalroot"),
    pytest.param(_bs("||?|Volume{01234567-89ab-cdef-0123-456789abcdef}|x"), id="volume-guid"),
]

REFUSED_ROOT_FORMS = [
    pytest.param(_bs("C:|"), id="drive-root-backslash"),
    pytest.param("C:/", id="drive-root-slash"),
    pytest.param("/", id="filesystem-root"),
    pytest.param("C:", id="drive-relative-bare"),
    pytest.param("C:x", id="drive-relative-name"),
    pytest.param(_bs("|x"), id="rooted-no-drive"),
    pytest.param("relative/dir", id="relative"),
    pytest.param("%SONDER_UNSET_ROOT_VAR%", id="unexpanded-percent-var"),
    pytest.param("$SONDER_UNSET_ROOT_VAR", id="unexpanded-dollar-var"),
    pytest.param(_bs("||?|C:|"), id="extended-drive-root"),
    pytest.param(_bs("||?|C:|x|..|.."), id="extended-dotdot-above-start"),
    pytest.param(_bs("C:|x|.."), id="dotdot-to-drive-root"),
]


def _is_network_spelling(value) -> bool:
    flat = os.fspath(value).replace("/", BS) if isinstance(value, (str, os.PathLike)) else ""
    return flat.startswith((BS * 2, BS + "??" + BS)) or flat == BS + "??"


@pytest.fixture
def no_network_touch(monkeypatch):
    """Fail the test if ``Path.resolve`` / ``os.stat`` / ``os.lstat`` / ``realpath`` see a UNC or device path."""
    touched: list[str] = []
    real_resolve = Path.resolve
    real_stat = os.stat
    real_lstat = os.lstat
    real_realpath = os.path.realpath

    def _check(value):
        if _is_network_spelling(value):
            touched.append(os.fspath(value))
            raise AssertionError("filesystem touched with a network/device path: %r" % os.fspath(value))

    def guarded_resolve(self, *args, **kwargs):
        _check(self)
        return real_resolve(self, *args, **kwargs)

    def guarded_stat(path, *args, **kwargs):
        _check(path)
        return real_stat(path, *args, **kwargs)

    def guarded_lstat(path, *args, **kwargs):
        _check(path)
        return real_lstat(path, *args, **kwargs)

    def guarded_realpath(path, *args, **kwargs):
        _check(path)
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    monkeypatch.setattr(os, "stat", guarded_stat)
    monkeypatch.setattr(os, "lstat", guarded_lstat)
    monkeypatch.setattr(os.path, "realpath", guarded_realpath)
    return touched


def _toml_literal(value: str) -> str:
    assert "'" not in value and "\n" not in value
    return "'" + value + "'"


def _write_config(tmp_path: Path, roots: list[str]) -> Path:
    path = tmp_path / "sonder.toml"
    body = "[state]\nworkspace_roots = [" + ", ".join(_toml_literal(r) for r in roots) + "]\n"
    path.write_text(body, encoding="utf-8")
    return path


def _root_errors(exc: runtime_config.ConfigError) -> list[str]:
    return [e for e in exc.errors if "workspace_roots" in e]


def _drive_of(path: Path) -> str:
    return path.drive[:1].upper()


@WINDOWS_ONLY
def test_toml_extended_local_root_is_stripped_and_accepted(tmp_path, no_network_touch):
    project = tmp_path / "proj"
    project.mkdir()
    toml = _write_config(tmp_path, [BS * 2 + "?" + BS + str(project)])

    config = runtime_config.load_config(toml, env={})

    assert config.state.workspace_roots == (str(project),)
    assert no_network_touch == []


@WINDOWS_ONLY
def test_toml_extended_local_slash_root_is_stripped_and_accepted(tmp_path, no_network_touch):
    project = tmp_path / "slashproj"
    project.mkdir()
    toml = _write_config(tmp_path, ["//?/" + project.as_posix()])

    config = runtime_config.load_config(toml, env={})

    assert config.state.workspace_roots == (str(project),)


@pytest.mark.parametrize("raw", REFUSED_PREFIXED_FORMS)
def test_toml_prefixed_root_is_refused_without_network_touch(tmp_path, no_network_touch, raw):
    toml = _write_config(tmp_path, [raw])

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(toml, env={})

    assert _root_errors(caught.value), caught.value.errors
    assert no_network_touch == []


@pytest.mark.parametrize("raw", REFUSED_PREFIXED_FORMS)
def test_toml_prefixed_root_next_to_a_good_root_is_still_refused(tmp_path, no_network_touch, raw):
    project = tmp_path / "goodproj"
    project.mkdir()
    toml = _write_config(tmp_path, [str(project), raw])

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(toml, env={})

    assert _root_errors(caught.value), caught.value.errors
    assert no_network_touch == []


@WINDOWS_ONLY
@pytest.mark.parametrize("raw", REFUSED_ROOT_FORMS)
def test_toml_drive_root_or_relative_root_is_refused(tmp_path, monkeypatch, raw):
    monkeypatch.delenv("SONDER_UNSET_ROOT_VAR", raising=False)
    toml = _write_config(tmp_path, [raw])

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(toml, env={})

    assert _root_errors(caught.value), caught.value.errors


def test_toml_posix_filesystem_root_is_refused(tmp_path):
    toml = _write_config(tmp_path, ["/"])

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(toml, env={})

    assert _root_errors(caught.value), caught.value.errors


def test_toml_normal_absolute_root_is_accepted(tmp_path, no_network_touch):
    project = tmp_path / "normalproj"
    project.mkdir()
    toml = _write_config(tmp_path, [str(project)])

    config = runtime_config.load_config(toml, env={})

    assert config.state.workspace_roots == (str(project),)


@WINDOWS_ONLY
def test_toml_root_on_another_drive_is_accepted(tmp_path, no_network_touch):
    project = tmp_path / "localproj"
    project.mkdir()
    other_drive = "D" if _drive_of(tmp_path) != "D" else "C"
    other = other_drive + ":" + BS + "sonder-other-drive-root-test" + BS + "otherproj"
    toml = _write_config(tmp_path, [str(project), other])

    config = runtime_config.load_config(toml, env={})

    assert config.state.workspace_roots == (str(project), other)


@pytest.mark.parametrize("raw", REFUSED_PREFIXED_FORMS)
def test_env_prefixed_root_is_refused_not_dropped(tmp_path, no_network_touch, raw):
    project = tmp_path / "envproj"
    project.mkdir()
    combined = os.pathsep.join([str(project), raw])

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(None, env={"SONDER_FILE_ROOTS": combined})

    assert _root_errors(caught.value), caught.value.errors
    assert no_network_touch == []


@pytest.mark.parametrize("raw", REFUSED_PREFIXED_FORMS)
def test_env_with_only_refused_roots_does_not_fall_back_to_toml_roots(tmp_path, no_network_touch, raw):
    project = tmp_path / "tomlproj"
    project.mkdir()
    toml = _write_config(tmp_path, [str(project)])

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(toml, env={"SONDER_FILE_ROOTS": raw})

    assert _root_errors(caught.value), caught.value.errors
    assert no_network_touch == []


@WINDOWS_ONLY
def test_env_extended_local_root_is_stripped_and_replaces_toml_roots(tmp_path, no_network_touch):
    toml_project = tmp_path / "tomlproj"
    env_project = tmp_path / "envproj"
    toml_project.mkdir()
    env_project.mkdir()
    toml = _write_config(tmp_path, [str(toml_project)])

    config = runtime_config.load_config(
        toml, env={"SONDER_FILE_ROOTS": BS * 2 + "?" + BS + str(env_project)},
    )

    assert config.state.workspace_roots == (str(env_project),)


@WINDOWS_ONLY
@pytest.mark.parametrize("raw", REFUSED_ROOT_FORMS)
def test_env_drive_root_or_relative_root_is_refused(monkeypatch, raw):
    monkeypatch.delenv("SONDER_UNSET_ROOT_VAR", raising=False)

    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(None, env={"SONDER_FILE_ROOTS": raw})

    assert _root_errors(caught.value), caught.value.errors


@pytest.mark.parametrize("raw", ["~", "~/work", "~" + BS + "work"], ids=["bare", "slash", "backslash"])
def test_tilde_root_is_not_expanded_by_config_and_is_refused(tmp_path, raw):
    with pytest.raises(runtime_config.ConfigError, match="not absolute"):
        runtime_config.load_config(_write_config(tmp_path, [raw]), env={})
    with pytest.raises(runtime_config.ConfigError, match="not absolute"):
        runtime_config.load_config(None, env={"SONDER_FILE_ROOTS": raw})


@pytest.mark.parametrize("raw", REFUSED_PREFIXED_FORMS)
def test_override_prefixed_root_is_refused_without_network_touch(no_network_touch, raw):
    with pytest.raises(runtime_config.ConfigError) as caught:
        runtime_config.load_config(None, env={}, overrides={"state.workspace_roots": raw})

    assert _root_errors(caught.value), caught.value.errors
    assert no_network_touch == []


@WINDOWS_ONLY
def test_override_extended_local_root_is_stripped_and_accepted(tmp_path, no_network_touch):
    project = tmp_path / "overrideproj"
    project.mkdir()

    config = runtime_config.load_config(
        None, env={}, overrides={"state.workspace_roots": BS * 2 + "?" + BS + str(project)},
    )

    assert config.state.workspace_roots == (str(project),)


@pytest.mark.parametrize("raw", REFUSED_PREFIXED_FORMS)
def test_validate_called_directly_never_resolves_a_refused_root(no_network_touch, raw):
    config = runtime_config.SonderConfig(
        state=runtime_config.StateConfig(workspace_roots=(raw,)),
    )
    errors: list[str] = []

    runtime_config._validate(config, errors)

    assert [e for e in errors if "workspace_roots" in e], errors
    assert no_network_touch == []
