"""Caller-supplied paths in UNC / device / NT spellings never reach the filesystem.

A ``stat`` (or ``lstat``, ``readlink``, ``_getfinalpathname``, ``resolve``) on
``\\\\host\\share\\x`` opens an SMB connection and can leak NTLM credentials
before any containment check refuses the path. Every entry point that takes a
caller path must run ``file_ops._reject_network_path`` on the raw text first.

Each test installs recorders on the filesystem primitives the guards use and
asserts both that the entry point refuses the spelling and that no recorded
call ever saw it. A recorded hit raises ``FileNotFoundError`` instead of
touching the real target, so a regression fails the test without opening a
network connection. The recorders are lexical and run on every OS.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
from pathlib import Path

import pytest

from sonder_runtime.adapters import archive_create, code_check
from sonder_runtime.adapters.agent_call_signature import call_signature
from sonder_runtime.adapters.claim_checker import ClaimChecker
from sonder_runtime.adapters.debugging.capture_source import GuardedCaptureSource
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.inspection import (
    archive_tools,
    content_digest,
    log_inspect,
    project_detect,
)
from sonder_runtime.adapters.security_eval.repo_reader import (
    ConfinedRepo,
    RepoRootRefused,
)
from sonder_runtime.bootstrap.claim_check_agent_tools import run_claim_check
from sonder_runtime.bootstrap.code_check_agent_tools import native_agent_assist
from sonder_runtime.domain.claim_check import CheckStatus
from sonder_runtime.domain.common.errors import SonderError
from sonder_runtime.domain.project_scope_keys import project_scoped_path_key
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig

pytestmark = pytest.mark.unit

FORMS = [
    r"\\host\share\x.dmp",
    r"\\?\UNC\host\share\x.dmp",
    r"\\.\pipe\x.dmp",
    r"\\?\GLOBALROOT\Device\Null",
    r"\\.\NUL",
    r"\\.\PhysicalDrive0",
    r"\\.\C:",
    "//host/share/x.dmp",
    "//./NUL",
    r"\??\C:\x.dmp",
]


# Each entry point's own refusal text, so a match proves the new guard refused,
# not ``_foreign_absolute`` (whose message also says "UNC").
GUARD_REFUSAL = "refusing a UNC or network path spelling"
REPO_REFUSAL = "UNC, device or network spelling"
LOG_REFUSAL = "must not use a UNC, device or network spelling"


def _flat(value: object) -> str | None:
    if isinstance(value, int):
        return None
    try:
        text = os.fsdecode(os.fspath(value))  # type: ignore[arg-type]
    except TypeError:
        return None
    return text.replace("/", "\\")


class Recorder:
    """Records (and blocks) every filesystem primitive call that names the probe target."""

    def __init__(self) -> None:
        self.target = ""
        self.calls: list[tuple[str, str]] = []

    def hit(self, value: object) -> bool:
        flat = _flat(value)
        if flat is None:
            return False
        if flat.startswith(("\\\\", "\\??\\")):
            return True
        target = self.target.replace("/", "\\")
        return bool(target) and target in flat

    def wrap(self, name: str, real):  # type: ignore[no-untyped-def]
        def recorder(*args, **kwargs):  # type: ignore[no-untyped-def]
            if args and self.hit(args[0]):
                self.calls.append((name, str(args[0])))
                raise FileNotFoundError(2, "blocked by test recorder", str(args[0]))
            return real(*args, **kwargs)
        return recorder


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    for name in ("stat", "lstat", "readlink", "scandir", "listdir", "open", "access"):
        monkeypatch.setattr(os, name, rec.wrap("os." + name, getattr(os, name)))
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", rec.wrap("open", real_open))
    monkeypatch.setattr(io, "open", rec.wrap("io.open", real_open))
    for name in ("realpath", "isdir", "isfile", "exists", "lexists", "islink", "samefile", "getsize"):
        real = getattr(os.path, name, None)
        if real is not None:
            monkeypatch.setattr(os.path, name, rec.wrap("os.path." + name, real))
    try:
        import nt  # type: ignore[import-not-found]
    except ImportError:
        nt = None
    import ntpath
    # Windows-only primitives; the ``_path_*`` helpers back os.path.is* on 3.12+.
    native = ("_getfinalpathname", "_path_isdir", "_path_isfile", "_path_exists",
              "_path_lexists", "_path_islink", "_path_isjunction")
    for module in (nt, ntpath):
        for name in native:
            if module is not None and hasattr(module, name):
                monkeypatch.setattr(module, name,
                                    rec.wrap(module.__name__ + "." + name, getattr(module, name)))
    real_resolve = pathlib.Path.resolve

    def resolve(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if rec.hit(self):
            rec.calls.append(("Path.resolve", str(self)))
            raise FileNotFoundError(2, "blocked by test recorder", str(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", resolve)
    return rec


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    root = (tmp_path / "allowed").resolve()
    root.mkdir()
    (root / "app.log").write_text("INFO ok\n", encoding="utf-8")
    (root / "sub").mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def _probe(recorder: Recorder, form: str) -> None:
    recorder.target = form
    recorder.calls.clear()


def test_recorder_is_live(recorder):
    """The recorders must actually intercept, or every 'no call' assertion is vacuous."""
    _probe(recorder, r"\\host\share\x.dmp")
    with pytest.raises(FileNotFoundError):
        os.lstat(r"\\host\share\x.dmp")
    with pytest.raises(FileNotFoundError):
        Path("//host/share/x.dmp").resolve()
    assert [name for name, _ in recorder.calls] == ["os.lstat", "Path.resolve"]


@pytest.mark.parametrize("form", FORMS)
def test_repository_read_path_refuses_before_filesystem(form, recorder, allowed):
    _probe(recorder, form)
    with pytest.raises(PermissionError, match=REPO_REFUSAL):
        file_ops.resolve_repository_read_path(form)
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_log_inspect_refuses_before_filesystem(form, recorder, allowed):
    _probe(recorder, form)
    with pytest.raises(log_inspect.LogInspectError, match=LOG_REFUSAL):
        log_inspect.resolve_log_path(form)
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_capture_source_dir_refuses_before_filesystem(form, recorder, allowed):
    source = GuardedCaptureSource()
    _probe(recorder, form)
    assert source.is_dir(form) is False
    with pytest.raises(SonderError):
        source.list_dir(form)
    with pytest.raises(SonderError):
        source.contained_file(form)
    assert recorder.calls == []


def test_capture_source_dir_accepts_plain_directory(recorder, allowed):
    """Positive control: the refusal above is the guard, not a broken is_dir."""
    _probe(recorder, r"\\host\share\x.dmp")
    assert GuardedCaptureSource().is_dir(str(allowed / "sub")) is True
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_inspection_roots_refuse_before_filesystem(form, recorder, allowed):
    calls = [
        lambda: archive_tools.list_archive(form),
        lambda: content_digest.digest_file(form),
        lambda: content_digest.digest_directory(form),
        lambda: project_detect.detect_project(form),
    ]
    for call in calls:
        _probe(recorder, form)
        with pytest.raises(PermissionError, match=GUARD_REFUSAL):
            call()
        assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_archive_create_paths_refuse_before_filesystem(form, recorder, allowed):
    _probe(recorder, form)
    with pytest.raises(PermissionError, match=GUARD_REFUSAL):
        archive_create._resolve_root(form, "")
    assert recorder.calls == []
    with pytest.raises(PermissionError, match=GUARD_REFUSAL):
        archive_create._resolve_destination(form, allowed, "", False)
    assert recorder.calls == []
    with pytest.raises(PermissionError, match=GUARD_REFUSAL):
        archive_create._create_archive_native(
            str(allowed), json.dumps([form]), "out.zip", developer_authorized=True)
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_code_check_project_root_refuses_before_filesystem(form, recorder, allowed):
    _probe(recorder, form)
    report = code_check.check_file(form, project_root=str(allowed))
    assert GUARD_REFUSAL in report
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_security_eval_repo_root_refuses_before_filesystem(form, recorder, allowed):
    _probe(recorder, form)
    # Either lexical refusal (3.14 no longer reads ``\??\C:`` as absolute) precedes any stat.
    with pytest.raises(RepoRootRefused, match="outside|absolute"):
        ConfinedRepo(form, [str(allowed)])
    assert recorder.calls == []


windows_only = pytest.mark.skipif(os.name != "nt", reason="extended-length paths are Windows syntax")


@windows_only
@pytest.mark.parametrize("prefix", ["\\\\?\\", "//?/"])
def test_extended_length_local_path_still_resolves(prefix, recorder, allowed):
    target = allowed / "app.log"
    spelled = prefix + str(target)
    _probe(recorder, spelled)
    assert file_ops.resolve_repository_read_path(spelled) == target.resolve()
    assert log_inspect.resolve_log_path(spelled) == target.resolve()
    assert GuardedCaptureSource().is_dir(prefix + str(allowed / "sub")) is True
    # The prefix is stripped before any filesystem call, so nothing saw it.
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_claim_checker_refuses_before_filesystem(form, recorder, allowed):
    checker = ClaimChecker(allowed)
    _probe(recorder, form)
    real, failure = checker._resolve(form)
    assert real is None
    assert failure is not None
    assert failure.status is CheckStatus.FAIL
    assert failure.detail == GUARD_REFUSAL
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_claim_check_tool_refuses_before_filesystem(form, recorder, allowed):
    """The model-facing route: a claim's ``file`` from ``claims_json`` reaches ``_resolve``."""
    _probe(recorder, form)
    result = run_claim_check(
        {"root": str(allowed), "claims_json": json.dumps([{"file": form, "text": "x"}])},
        config=ClaimCheckConfig())
    assert GUARD_REFUSAL in json.dumps(result)
    assert recorder.calls == []


def test_claim_checker_accepts_plain_file(recorder, allowed):
    """Positive control: a file inside the root still resolves."""
    _probe(recorder, r"\\host\share\x.dmp")
    real, failure = ClaimChecker(allowed)._resolve("app.log")
    assert failure is None
    assert real is not None and Path(real) == (allowed / "app.log").resolve()
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_native_file_check_refuses_before_filesystem(form, recorder, allowed):
    """The native MCP ``file_check`` route resolves against workspace roots before ``check_file``."""
    _probe(recorder, form)
    result = native_agent_assist("file_check", {"path": form}, None, (str(allowed),))
    assert result["isError"] is True
    assert result["output"].endswith("file_check refuses a UNC or network path spelling")
    assert recorder.calls == []


def test_native_file_check_accepts_plain_file(recorder, allowed, monkeypatch):
    """Positive control: the refusal above is the guard, not a broken handler."""
    import permission_modes
    monkeypatch.setattr(permission_modes, "EXECUTION_TOOLS",
                        permission_modes.EXECUTION_TOOLS | {"file_check"})
    (allowed / "ok.json").write_text("{}", encoding="utf-8")
    _probe(recorder, r"\\host\share\x.dmp")
    result = native_agent_assist("file_check", {"path": "ok.json"}, None, (str(allowed),))
    assert result["output"] == "file_check ok.json: none"
    assert recorder.calls == []


@windows_only
@pytest.mark.parametrize("prefix", ["\\\\?\\", "//?/"])
def test_extended_length_local_path_reaches_claim_checker(prefix, recorder, allowed):
    target = allowed / "app.log"
    _probe(recorder, prefix + str(target))
    real, failure = ClaimChecker(allowed)._resolve(prefix + str(target))
    assert failure is None
    assert real is not None and Path(real) == target.resolve()
    assert recorder.calls == []


# --------------------------------------------------------------------------- agent call signature
# The agent loop computes a de-duplication signature from model-supplied
# arguments (server._agent_call_signature) before any path policy check runs,
# so the signature itself must never resolve a network spelling.

_SCOPED = frozenset({"log_inspect", "directory_digest", "project_detect", "file_read", "archive_list"})

SIGNATURE_ARGS = [
    ("log_inspect", "path"),
    ("directory_digest", "path"),
    ("project_detect", "path"),
    ("archive_list", "path"),
    ("file_copy", "source"),
    ("file_copy", "destination"),
    ("file_move", "source"),
    ("data_convert", "input_path"),
    ("data_convert", "output_path"),
    ("workspace_run", "cwd"),
    ("script_run", "path"),
    ("script_run", "cwd"),
]


def _signature(tool: str, args: dict) -> tuple[str, str]:
    return call_signature(tool, args, project_scoped_path_tools=_SCOPED,
                          project_scoped_path_key=project_scoped_path_key)


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize(("tool", "key"), SIGNATURE_ARGS)
def test_call_signature_never_resolves_a_network_spelling(tool, key, form, recorder):
    if tool in _SCOPED:
        key = project_scoped_path_key(tool)
    _probe(recorder, form)
    name, payload = _signature(tool, {key: form})
    assert name == tool
    assert json.loads(payload)[key] == os.path.normcase(os.path.normpath(form))
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_archive_create_signature_never_resolves_a_network_spelling(form, recorder, allowed):
    _probe(recorder, form)
    for args in (
        {"root": form, "destination": "out.zip", "inputs_json": ["a.txt"]},
        {"root": str(allowed), "destination": form, "inputs_json": [form]},
    ):
        _signature("archive_create", args)
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_server_agent_call_signature_never_resolves_a_network_spelling(form, recorder):
    """The real dispatcher tables: log_inspect is named entry point #1 of this fix."""
    import server
    _probe(recorder, form)
    for tool, key in (("log_inspect", server._project_scoped_path_key("log_inspect")),
                      ("directory_digest", server._project_scoped_path_key("directory_digest")),
                      ("workspace_run", "cwd"), ("data_convert", "input_path")):
        server._agent_call_signature(tool, {key: form})
    server._agent_call_signature("archive_create", {"root": form, "inputs_json": [form]})
    assert recorder.calls == []


def test_call_signature_still_resolves_local_spellings(allowed, recorder):
    """Positive control: equivalent local spellings still share one signature."""
    _probe(recorder, r"\\host\share\x.dmp")
    plain = _signature("log_inspect", {"path": str(allowed / "app.log")})
    dotted = _signature("log_inspect", {"path": str(allowed / "sub" / ".." / "app.log")})
    assert plain == dotted
    assert json.loads(plain[1])["path"] == os.path.normcase(os.path.realpath(allowed / "app.log"))
    assert recorder.calls == []


@windows_only
@pytest.mark.parametrize("prefix", ["\\\\?\\", "//?/"])
def test_call_signature_resolves_extended_length_local_path(prefix, allowed, recorder):
    _probe(recorder, r"\\host\share\x.dmp")
    extended = _signature("log_inspect", {"path": prefix + str(allowed / "app.log")})
    assert extended == _signature("log_inspect", {"path": str(allowed / "app.log")})
    assert recorder.calls == []
