"""Agent-loop policy checks refuse UNC / device / NT path spellings before any filesystem call.

A ``stat``, ``realpath`` or ``Path.resolve`` on ``\\\\host\\share\\x`` opens an
SMB connection on Windows and can leak NTLM credentials before a containment
check refuses the path.  The host policy functions that vet model-supplied
paths in the agent loop (``_repository_scope_path_error``,
``_agent_project_execution_argument_error``, ``_selfmod_agent_policy``, the
restricted lane's ``RestrictedRun.path_refusal``) and the speculative file
prefetcher must decide on the spelling alone.

Each test installs recorders on the filesystem primitives and asserts that the
check refuses the spelling and that no recorded call ever saw it.  A recorded
hit raises ``FileNotFoundError`` instead of touching the real target, so a
regression fails the test without opening a network connection.  The
recorders are lexical and run on every OS.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib
from pathlib import Path

import pytest

import server
import sonder_speculation
from sonder_runtime.adapters import restricted_tool_lane as lane

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
BACKSLASH_FORMS = [form for form in FORMS if not form.startswith("/")]
REFUSAL = "UNC, device or network spelling"


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
    native = ("_getfinalpathname", "_getfinalpathname_nonstrict", "_findfirstfile",
              "_path_isdir", "_path_isfile", "_path_exists",
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
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("form", FORMS)
def test_recorder_is_live(recorder, form):
    with pytest.raises(FileNotFoundError):
        Path(form).resolve(strict=False)
    with pytest.raises(FileNotFoundError):
        os.path.realpath(form)
    assert [name for name, _ in recorder.calls] == ["Path.resolve", "os.path.realpath"]


# -- _repository_scope_path_error -------------------------------------------

def _scope_cases(form: str) -> list[tuple[str, dict]]:
    return [
        ("file_read", {"path": form}),
        ("workspace_run", {"cwd": form}),
        ("script_run", {"path": "run.py", "cwd": form}),
        ("archive_create", {"root": form, "destination": "o.zip", "inputs_json": '["a"]'}),
        ("archive_create", {"root": ".", "destination": "o.zip", "inputs_json": json.dumps([form])}),
        ("archive_create", {"root": ".", "destination": form, "inputs_json": '["a"]'}),
        ("repo_diff", {"root": ".", "path": form}),
    ]


@pytest.mark.parametrize("form", FORMS)
def test_repository_scope_refuses_network_spellings_before_any_fs_call(recorder, project, form):
    for tool_name, args in _scope_cases(form):
        result = server._repository_scope_path_error(tool_name, args, str(project))
        assert REFUSAL in result, (tool_name, args, result)
        assert recorder.calls == [], (tool_name, recorder.calls)


@pytest.mark.parametrize("form", FORMS)
def test_repository_read_only_policy_refuses_before_any_fs_call(recorder, project, form):
    result = server._repository_read_only_error(
        "file_read", {"path": form}, trusted_extra_roots=str(project),
    )
    assert REFUSAL in result
    assert recorder.calls == []


@pytest.mark.parametrize("form", FORMS)
def test_prefetched_network_read_is_refused_by_the_scope_check(recorder, project, form):
    # The speculative prefetch path: predicted args are scoped, then dispatch
    # runs the same scope check first.
    scoped = server._project_scope_args("file_read", {"path": form}, str(project))
    result = server._repository_scope_path_error("file_read", scoped, str(project))
    assert REFUSAL in result
    assert recorder.calls == []


def test_repository_scope_keeps_local_paths(recorder, project):
    for tool_name, args in [
        ("file_read", {"path": "src/a.py"}),
        ("file_read", {"path": str(project / "src" / "a.py")}),
        ("workspace_run", {"cwd": "."}),
        ("archive_create", {"root": ".", "destination": "o.zip", "inputs_json": '["src/a.py"]'}),
        ("repo_diff", {"root": ".", "path": "src/a.py"}),
    ]:
        assert server._repository_scope_path_error(tool_name, args, str(project)) == "", (tool_name, args)
    outside = server._repository_scope_path_error("file_read", {"path": "../elsewhere.txt"}, str(project))
    assert "outside the host-selected project root" in outside
    assert recorder.calls == []


@pytest.mark.skipif(os.name != "nt", reason="extended-length drive paths are a Windows spelling")
def test_repository_scope_keeps_extended_length_local_paths(recorder, project):
    extended = "\\\\?\\" + str(project / "src" / "a.py")
    assert server._repository_scope_path_error("file_read", {"path": extended}, str(project)) == ""


# -- _agent_project_execution_argument_error ---------------------------------

def _execution_cases(form: str, project: Path) -> list[tuple[str, dict]]:
    return [
        ("workspace_run", {"program": "git", "args": ["status", form], "cwd": str(project)}),
        ("workspace_run", {"program": "git", "args": ["--out=" + form], "cwd": str(project)}),
        ("workspace_run", {"program": "git", "args": ["@" + form], "cwd": str(project)}),
        ("workspace_run", {"program": "git", "args": ["x.txt"], "cwd": form}),
        ("script_run", {"path": form + "/s.py", "args": []}),
        ("build_run", {"command": "make " + form, "root": "."}),
        ("build_run", {"command": "make all", "root": form}),
    ]


@pytest.mark.parametrize("form", FORMS)
def test_execution_guard_refuses_network_spellings_before_any_fs_call(recorder, project, form):
    for tool_name, args in _execution_cases(form, project):
        result = server._agent_project_execution_argument_error(tool_name, args, str(project))
        assert REFUSAL in result, (tool_name, args, result)
        assert recorder.calls == [], (tool_name, recorder.calls)


@pytest.mark.parametrize("form", BACKSLASH_FORMS)
def test_execution_guard_refuses_an_embedded_network_spelling_whole(recorder, project, form):
    for token in ("-I" + form, "/Fo" + form, "x=" + form):
        result = server._agent_project_execution_argument_error(
            "workspace_run", {"program": "cl", "args": [token], "cwd": str(project)}, str(project),
        )
        assert REFUSAL in result, (token, result)
    assert recorder.calls == []


def test_execution_guard_keeps_local_paths(recorder, project):
    args = {"program": "git", "args": ["status", "src/a.py", "--out=build/x.o"], "cwd": str(project)}
    assert server._agent_project_execution_argument_error("workspace_run", args, str(project)) == ""
    assert server._agent_project_execution_argument_error(
        "build_run", {"command": "make src/a.py", "root": str(project)}, str(project),
    ) == ""
    outside = server._agent_project_execution_argument_error(
        "workspace_run", {"program": "git", "args": ["../elsewhere.txt"], "cwd": str(project)}, str(project),
    )
    assert "outside the host-selected project root" in outside
    assert recorder.calls == []


def test_execution_guard_never_raises_when_resolve_fails(recorder, project):
    recorder.target = "boom.txt"
    result = server._agent_project_execution_argument_error(
        "workspace_run", {"program": "git", "args": ["boom.txt"], "cwd": str(project)}, str(project),
    )
    assert result.startswith("ERROR: agent project execution rejected")
    assert recorder.calls  # the failing resolve was reached and did not escape


# -- _selfmod_agent_policy ----------------------------------------------------

def _selfmod_policy(workspace: Path):  # type: ignore[no-untyped-def]
    return server._selfmod_agent_policy({
        "workspace_path": str(workspace),
        "files": ["src/a.py"],
        "budgets": {"max_tool_calls": 1000, "max_runtime_seconds": 600, "max_files_inspected": 1000},
    })


@pytest.mark.parametrize("form", FORMS)
def test_selfmod_policy_refuses_network_spellings_before_any_fs_call(recorder, project, form):
    policy = _selfmod_policy(project)
    for tool_name, args in [
        ("workspace_run", {"cwd": form, "args_json": ["python", "-V"]}),
        ("file_read", {"path": form}),
        ("file_edit", {"path": form}),
        ("file_find", {"root": form}),
    ]:
        result = policy(tool_name, dict(args))
        assert REFUSAL in result, (tool_name, args, result)
        assert recorder.calls == [], (tool_name, recorder.calls)


def test_selfmod_policy_keeps_local_paths(recorder, project):
    policy = _selfmod_policy(project)
    assert policy("workspace_run", {"cwd": str(project), "args_json": ["python", "-V"]}) == ""
    args = {"path": "src/a.py"}
    assert policy("file_read", args) == ""
    assert Path(args["path"]) == (project / "src" / "a.py").resolve()
    assert "outside the isolated candidate workspace" in policy("file_read", {"path": "../x.txt"})
    assert recorder.calls == []


# -- restricted lane ----------------------------------------------------------

def _restricted(project: Path) -> lane.RestrictedRun:
    run = lane.RestrictedRun(
        lane="agent", model="m", tools=frozenset({"file_read", "text_search"}), profiles=("p",),
        secret_globs=(".env",), caller_project="", target=("x", False, None, "model:m"),
    )
    run.project_root = str(project)
    return run


@pytest.mark.parametrize("form", FORMS)
def test_restricted_lane_refuses_network_spellings_before_any_fs_call(recorder, project, monkeypatch, form):
    run = _restricted(project)
    assert REFUSAL in run.path_refusal("file_read", {"path": form}, ())
    assert run._relative(form) is None
    assert run._secret_or_outside(str(project), form, ()) is True
    monkeypatch.setattr(run, "current_tools", lambda policy=None: run.tools)
    monkeypatch.setattr(run, "current_secret_globs", lambda: ())
    check = run.tool_policy(None, lambda tool_name, args, root: args)
    assert REFUSAL in check("file_read", {"path": form})
    assert REFUSAL in check("text_search", {"query": "q", "root": form})
    assert recorder.calls == []


def test_restricted_lane_keeps_local_paths(recorder, project):
    run = _restricted(project)
    root = os.path.realpath(project)
    run.project_root = root
    assert run.path_refusal("file_read", {"path": "src/a.py"}, ()) == ""
    assert run._relative("src/a.py") == "src/a.py"
    assert run._secret_or_outside(root, "src/a.py", ()) is False
    assert "outside the project root" in run.path_refusal("file_read", {"path": "../x.txt"}, ())
    assert recorder.calls == []


# -- speculative prefetcher ---------------------------------------------------

@pytest.mark.parametrize("form", FORMS)
def test_prefetcher_never_predicts_a_network_spelling(recorder, form):
    named = form if "." in form.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] else form + ".txt"
    listing = "\n".join([
        "  12  %s" % named,
        "  [F] %s (10 bytes)" % named,
        "file %s (10 bytes)" % named,
        "  13  src/a.py",
    ])
    assert sonder_speculation.FilePrefetcher.extract_paths(listing) == ["src/a.py"]
    prefetcher = sonder_speculation.FilePrefetcher()
    prefetcher.observe("script_search", {}, "  11  src/b.py\n  12  %s\n" % named)
    prefetcher.observe("file_read", {"path": "src/b.py"}, "ok")
    assert prefetcher.predict_read() is None
    assert recorder.calls == []


# -- served_work_project ------------------------------------------------------

@pytest.mark.parametrize("form", FORMS)
def test_served_work_project_refuses_network_spellings_before_any_fs_call(
    recorder, monkeypatch, tmp_path, form,
):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    assert server.served_work_project(form) == ""
    assert server.served_work_project(form.replace("\\", "/")) == ""
    assert recorder.calls == []


def test_served_work_project_keeps_local_directories(recorder, monkeypatch, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    assert server.served_work_project(str(project)) == str(project.resolve())
    assert recorder.calls == []


# -- batching-guard target identity -------------------------------------------

@pytest.mark.parametrize("form", FORMS)
def test_batch_target_identity_never_resolves_a_network_spelling(recorder, form):
    assert server._agent_batch_target_identity(form) == form
    assert server._agent_batch_target_identity("  %s  " % form) == form
    assert recorder.calls == []


# -- forward-slash UNC glued to an option -------------------------------------

FORWARD_EMBEDDED = [
    "-I//attacker/share",
    "-I//attacker/share/inc",
    "/Fo//attacker/share/x.obj",
    "x=//attacker/share/x",
    "-Wl,//attacker/share/x",
    "a.jar;//attacker/share/b.jar",
    "-I//./pipe/x",
    r"-I\/attacker/share",
]


@pytest.mark.parametrize("token", FORWARD_EMBEDDED)
def test_forward_slash_unc_glued_to_an_option_is_a_network_spelling(token):
    from sonder_runtime.domain import network_path_spelling as spelling

    assert spelling.contains_network_spelling(token)


@pytest.mark.parametrize("token", [
    "a//b/c", "src//x.py", "-Iinclude", "-I../inc", "--url=https://h/x",
    "-Dhttps://example.invalid/x", "-I//?/C:/x", "/Zi",
])
def test_ordinary_tokens_are_not_network_spellings(token):
    from sonder_runtime.domain import network_path_spelling as spelling

    assert not spelling.contains_network_spelling(token)


@pytest.mark.parametrize("token", [
    "-I//host/share/x=y",
    "/I//host/share/x=y",
    "-I//attacker/share/inc=x",
    r"-I\\host\share\x=y",
    r"/I\\host\share\a=b",
])
def test_execution_guard_refuses_a_glued_unc_with_a_trailing_equals(
    recorder, project, token,
):
    # Splitting the token at its first `=` used to leave only the suffix
    # ("y") for the network check, while the tool reads the whole value.
    result = server._agent_project_execution_argument_error(
        "workspace_run", {"program": "cl", "args": [token], "cwd": str(project)},
        str(project),
    )
    assert REFUSAL in result, (token, result)
    assert recorder.calls == []


@pytest.mark.parametrize("token", [
    "-D%//example.invalid/x",
    "-I$//host/share",
    pytest.param("/I%//host/share", marks=pytest.mark.skipif(
        os.name != "nt", reason="slash switches are Windows-only",
    )),
])
def test_execution_guard_refuses_a_unc_reached_from_a_glued_start(recorder, project, token):
    # The whole token is not a network spelling, but one start position of
    # the glued-path expansion is "//host/share"; it must be refused, never
    # resolved.
    result = server._agent_project_execution_argument_error(
        "workspace_run", {"program": "gcc", "args": [token], "cwd": str(project)},
        str(project),
    )
    assert REFUSAL in result, (token, result)
    assert recorder.calls == []


@pytest.mark.parametrize("token", [
    "-Dhttps://example.invalid/x",
    "-DURL=https://example.invalid/x",
    "--url=https://example.invalid/x",
])
def test_execution_guard_never_resolves_a_unc_cut_out_of_a_url(recorder, project, token):
    # No start position of the glued-path expansion may hand a UNC spelling
    # cut out of a URL to resolve.
    server._agent_project_execution_argument_error(
        "workspace_run", {"program": "gcc", "args": [token], "cwd": str(project)},
        str(project),
    )
    assert recorder.calls == []


@pytest.mark.parametrize("token", FORWARD_EMBEDDED)
def test_execution_guard_refuses_a_forward_slash_unc_glued_to_an_option(
    recorder, project, token,
):
    result = server._agent_project_execution_argument_error(
        "workspace_run", {"program": "cl", "args": [token], "cwd": str(project)},
        str(project),
    )
    assert REFUSAL in result, (token, result)
    assert recorder.calls == []
