"""Scrubbed build environments and vcvars capture with Windows fakes.

No process runs: ``run_bounded`` is a fake that asserts on the environment
it receives, and the vswhere record is a fixed Windows-shaped record.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.build.environment import (
    WRAPPER_BYTES,
    WRAPPER_NAME,
    JsonEnvCache,
    ScrubbedEnvironmentProvider,
    VcvarsCapture,
    passthrough_allowed,
)
from sonder_runtime.domain.common.errors import SonderError

pytestmark = pytest.mark.unit

VS = r"C:\Program Files\Microsoft Visual Studio\2022\Community"
VCVARS = VS + r"\VC\Auxiliary\Build\vcvars64.bat"
CL = VS + r"\VC\Tools\MSVC\14.40.33807\bin\Hostx64\x64\cl.exe"
SDK = r"C:\Program Files (x86)\Windows Kits\10"
FAKE_SECRET = "pretend-credential-value"


@dataclass(frozen=True)
class Record:
    name: str
    path: str
    version: str = "14.40.33807"
    details: tuple = ()


class Lookup:
    def __init__(self, record):
        self.record = record

    def lookup(self, name):
        return self.record if name == "cl" else None


def cl_record(vcvars=VCVARS, path=CL):
    return Record("cl", path, details=(("vs_version", "17.10"), ("toolset", "14.40.33807"),
                                       ("vcvars64", vcvars)))


HOST_ENV = {
    "SystemRoot": r"C:\Windows", "ComSpec": r"C:\Windows\System32\cmd.exe",
    "PATH": r"C:\Windows\System32;C:\Tools\cmake\bin;relative\dir;%EVIL%\bin",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD", "TEMP": r"C:\Users\op\AppData\Local\Temp",
    "USERPROFILE": r"C:\Users\op", "OPENAI_API_KEY": FAKE_SECRET, "GITHUB_TOKEN": FAKE_SECRET,
    "HTTPS_PROXY": "http://proxy:8080", "_NT_SYMBOL_PATH": "srv*https://msdl", "MY_SDK_ROOT": r"D:\sdk",
    "NUGET_PACKAGES": r"D:\nuget", "ProgramFiles": r"C:\Program Files",
}

SET_OUTPUT = "\r\n".join([
    "ALLUSERSPROFILE=C:\\ProgramData",
    "INCLUDE=%s\\VC\\Tools\\MSVC\\14.40.33807\\include;%s\\Include\\10.0.22621.0\\ucrt" % (VS, SDK),
    "LIB=%s\\VC\\Tools\\MSVC\\14.40.33807\\lib\\x64" % VS,
    "LIBPATH=%s\\VC\\Tools\\MSVC\\14.40.33807\\lib\\x64" % VS,
    "Path=%s\\VC\\Tools\\MSVC\\14.40.33807\\bin\\Hostx64\\x64;%s\\bin\\10.0.22621.0\\x64;"
    "C:\\Windows\\System32;C:\\Users\\op\\evil;\\\\server\\share\\bin" % (VS, SDK),
    "VCToolsVersion=14.40.33807",
    "WindowsSdkDir=%s\\" % SDK,
    "OPENAI_API_KEY=" + FAKE_SECRET,
    "SONDER_VCVARS_BAT=" + VCVARS,
    "Platform=x64",
])


class FakeRun:
    def __init__(self, provider, output=SET_OUTPUT, outcome="ok"):
        self.provider = provider
        self.output = output
        self.outcome = outcome
        self.calls = []

    def __call__(self, argv, *, timeout_seconds, max_output_chars, env, cwd=None):
        self.calls.append((tuple(argv), dict(env)))
        expected = dict(self.provider.base())
        extra = {key: env[key] for key in env if key not in expected}
        assert set(extra) == {"SONDER_VCVARS_BAT", "SONDER_VCVARS_ARGS"}
        assert {key: env[key] for key in expected} == expected
        assert FAKE_SECRET not in "".join(env.values())
        assert timeout_seconds <= 30 and max_output_chars <= 256 * 1024
        return SimpleNamespace(outcome=self.outcome, output=self.output, exit_code=0)


class Stat:
    st_size = 1234
    st_mtime = 1.0
    st_mtime_ns = 1_000_000_000


def build(tmp_path, record=None, output=SET_OUTPUT, outcome="ok", passthrough=("MY_SDK_ROOT",)):
    provider = ScrubbedEnvironmentProvider(host="windows", source=lambda: dict(HOST_ENV),
                                           passthrough=passthrough, project_local=lambda p: False)
    run = FakeRun(provider, output, outcome)
    capture = VcvarsCapture(str(tmp_path), lookup=Lookup(record or cl_record()),
                            base_environment=provider.base, system_root=lambda: r"C:\Windows",
                            run=run, stat_file=lambda path: Stat(), snapshot_digest=lambda: "snap1",
                            clock=lambda: 5.0)
    provider.use_vcvars(capture)
    return provider, capture, run


def test_the_env_given_to_vcvars_is_the_base_plus_two_keys(tmp_path):
    provider, capture, run = build(tmp_path)
    env = provider.environment(system="ninja", family="msvc", toolchain_hint="14.40.33807")
    assert env.source == "vcvars" and not env.cache_hit
    argv, sent = run.calls[0]
    assert argv == (r"C:\Windows\System32\cmd.exe", "/d", "/c", str(capture.wrapper_path))
    assert sent["SONDER_VCVARS_BAT"] == VCVARS
    assert sent["SONDER_VCVARS_ARGS"] == "-vcvars_ver=14.40.33807"
    values = dict(env.pairs)
    assert FAKE_SECRET not in "".join(values.values())
    assert values["VSLANG"] == "1033"
    assert "HTTPS_PROXY" not in values and "_NT_SYMBOL_PATH" not in values
    assert "NUGET_PACKAGES" not in values and values["MY_SDK_ROOT"] == r"D:\sdk"
    assert "SONDER_VCVARS_BAT" not in values  # not an allowlisted vcvars key
    assert values["VCToolsVersion"] == "14.40.33807"


def test_path_entries_outside_vs_and_sdk_roots_are_dropped(tmp_path):
    provider, _, _ = build(tmp_path)
    values = dict(provider.environment(system="ninja", family="msvc").pairs)
    path = values["Path"].split(";")
    assert any(entry.startswith(VS) for entry in path)
    assert any(entry.startswith(SDK) for entry in path)
    assert r"C:\Windows\System32" in path
    assert r"C:\Users\op\evil" not in path and not any(entry.startswith("\\\\") for entry in path)
    assert "PATH" not in values  # replaced case-insensitively, one key only


def test_the_wrapper_is_written_once_with_fixed_bytes_and_tamper_is_refused(tmp_path):
    provider, capture, run = build(tmp_path)
    provider.environment(system="ninja", family="msvc")
    wrapper = capture.wrapper_path
    assert wrapper.name == WRAPPER_NAME and wrapper.read_bytes() == WRAPPER_BYTES
    if os.name != "nt":
        assert wrapper.stat().st_mode & 0o777 == 0o700
    wrapper.write_bytes(WRAPPER_BYTES + b"calc.exe\r\n")
    JsonEnvCache(str(tmp_path / "build-env" / "vcvars-cache.json")).clear()
    with pytest.raises(SonderError) as excinfo:
        provider.environment(system="ninja", family="msvc")
    assert excinfo.value.code == "ENV_CAPTURE_FAILED"
    assert len(run.calls) == 1  # the tampered wrapper never ran


@pytest.mark.parametrize("vcvars", [
    r"C:\VS&calc\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\VS%PATH%\VC\Auxiliary\Build\vcvars64.bat",
    r"C:\Other\vcvars64.bat",
    r"\\server\share\VC\Auxiliary\Build\vcvars64.bat",
    r"D:\Elsewhere\VC\Auxiliary\Build\vcvars64.bat",  # not the compiler's installation
])
def test_unsafe_vcvars_paths_are_refused(tmp_path, vcvars):
    provider, _, run = build(tmp_path, record=cl_record(vcvars=vcvars))
    with pytest.raises(SonderError) as excinfo:
        provider.environment(system="ninja", family="msvc")
    assert excinfo.value.code == "ENV_CAPTURE_FAILED" and not run.calls


def test_an_unsafe_toolset_hint_is_ignored_not_passed(tmp_path):
    provider, _, run = build(tmp_path)
    provider.environment(system="ninja", family="msvc", toolchain_hint="14.4 & calc")
    assert run.calls[0][1]["SONDER_VCVARS_ARGS"] == ""


def test_cache_hit_miss_and_invalidation(tmp_path):
    provider, capture, run = build(tmp_path)
    first = provider.environment(system="ninja", family="msvc")
    second = provider.environment(system="ninja", family="msvc")
    assert not first.cache_hit and second.cache_hit and len(run.calls) == 1
    assert first.pairs == second.pairs
    cache_file = tmp_path / "build-env" / "vcvars-cache.json"
    if os.name != "nt":
        assert cache_file.stat().st_mode & 0o777 == 0o600
    # a different vswhere snapshot (toolset upgrade) misses
    capture._snapshot_digest = lambda: "snap2"
    third = provider.environment(system="ninja", family="msvc")
    assert not third.cache_hit and len(run.calls) == 2
    # a cache file readable by others is not trusted
    if os.name != "nt":
        os.chmod(cache_file, 0o644)
        fourth = provider.environment(system="ninja", family="msvc")
        assert not fourth.cache_hit and len(run.calls) == 3


def test_a_failed_capture_and_missing_include_are_env_capture_failed(tmp_path):
    for output, outcome in ((SET_OUTPUT, "timeout"), ("Path=%s\\bin" % VS, "ok")):
        provider, _, _ = build(tmp_path / outcome, output=output, outcome=outcome)
        with pytest.raises(SonderError) as excinfo:
            provider.environment(system="ninja", family="msvc")
        assert excinfo.value.code == "ENV_CAPTURE_FAILED"


def test_msbuild_and_gnu_families_do_not_capture(tmp_path):
    provider, _, run = build(tmp_path)
    env = provider.environment(system="msbuild", family="")
    assert env.source == "scrubbed" and not run.calls
    assert dict(env.pairs)["PATH"].startswith(r"C:\Windows\System32")
    assert "relative" not in dict(env.pairs)["PATH"] and "%EVIL%" not in dict(env.pairs)["PATH"]


def test_posix_base_is_scrubbed(tmp_path):
    source = {"PATH": "/usr/bin:relative:/nonexistent-dir:" + str(tmp_path), "HOME": "/root",
              "LD_PRELOAD": "/tmp/x.so", "AWS_SECRET_ACCESS_KEY": FAKE_SECRET, "CCACHE_DIR": "/c",
              "http_proxy": "http://p"}
    provider = ScrubbedEnvironmentProvider(
        host="posix", source=lambda: source,
        passthrough=("CCACHE_DIR", "LD_PRELOAD", "http_proxy", "AWS_SECRET_ACCESS_KEY"),
        project_local=lambda path: path == str(tmp_path))
    values = dict(provider.environment(system="cmake", family="gnu").pairs)
    assert values["PATH"] == "/usr/bin"
    assert values["LC_ALL"] == "C.UTF-8" and values["CCACHE_DIR"] == "/c"
    for key in ("LD_PRELOAD", "http_proxy", "AWS_SECRET_ACCESS_KEY"):
        assert key not in values
    assert FAKE_SECRET not in "".join(values.values())


@pytest.mark.parametrize("name,allowed", [
    ("CCACHE_DIR", True), ("VULKAN_SDK", True), ("LD_PRELOAD", False), ("HTTPS_PROXY", False),
    ("no_proxy", False), ("SYMSRV_CACHE", False), ("NUGET_FEED", False), ("SONDER_API_KEY", False),
    ("MAKEFLAGS", False), ("CL", False), ("DYLD_INSERT_LIBRARIES", False), ("BAD NAME", False),
])
def test_passthrough_denylist(name, allowed):
    assert passthrough_allowed(name) is allowed


def test_wrapper_digest_is_pinned():
    assert hashlib.sha256(WRAPPER_BYTES).hexdigest() == \
        hashlib.sha256(b'@echo off & call "%SONDER_VCVARS_BAT%" %SONDER_VCVARS_ARGS% '
                       b'>nul 2>&1 || exit /b 1 & set\r\n').hexdigest()
