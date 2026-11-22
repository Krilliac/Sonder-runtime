"""Bounds on workbench.run_program's inline-code guard, its process teardown,
and text_search's regex execution.

* Interpreter launchers (``py``, ``env``) and pwsh's ``-CommandWithArgs`` must
  not carry inline code past the guard.
* Clustered short options (``python -Ic``, ``bash -ec``, ``perl -le``) are
  inline code too.
* An interpreter must not be handed caller stdin as its program, while a
  script run through run_script keeps its own ``-c``-style arguments.
* Descendants a program leaves behind are killed when the program exits, and
  a leader that will not die is reported, not raised.
* A catastrophic regex is held to the search's wall budget.
"""

import inspect
import os
import sys
import textwrap
import time

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench


def _guard_root(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")


def _fake_program(monkeypatch, tmp_path, filename):
    """Resolve every PATH lookup to a non-existent ``filename`` under tmp_path.

    The guard must refuse before anything is launched, so the program never
    has to exist; a guard that lets the call through fails on the missing
    file with FileNotFoundError instead of PermissionError.
    """
    fake = str(tmp_path / "bin" / filename)
    monkeypatch.setattr(workbench.shutil, "which", lambda name: fake)
    return fake


# --- launchers and pwsh -CommandWithArgs -----------------------------------

@pytest.mark.parametrize("program,filename,args", [
    ("py", "py.exe", ["-c", "print(1)"]),
    ("py", "py.exe", ["-3.12", "-c", "print(1)"]),
    ("pyw", "pyw.exe", ["-c", "print(1)"]),
    ("env", "env", ["python", "-c", "print(1)"]),
    ("env", "env.exe", ["-i", "bash", "-c", "id"]),
    ("nice", "nice", ["bash", "-c", "id"]),
    ("timeout", "timeout", ["5", "python3", "-c", "print(1)"]),
    ("xargs", "xargs", ["sh", "-c", "id"]),
])
def test_run_program_refuses_inline_code_through_launchers(
    monkeypatch, tmp_path, program, filename, args,
):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, filename)
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(program, args_json=args, timeout=5)


@pytest.mark.parametrize("flag", ["-cwa", "-CommandWithArgs", "-commandw", "-CWA"])
@pytest.mark.parametrize("filename", ["pwsh.exe", "pwsh", "powershell.exe"])
def test_run_program_refuses_pwsh_command_with_args(monkeypatch, tmp_path, flag, filename):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, filename)
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program("pwsh", args_json=[flag, "Write-Output $args", "x"])


def test_inline_interpreter_key_covers_the_py_launchers():
    assert workbench._inline_interpreter_key("py.exe") == "python"
    assert workbench._inline_interpreter_key("py") == "python"
    assert workbench._inline_interpreter_key("pyw.exe") == "python"
    # Not a prefix match: other "py..." tools are not the Python launcher.
    assert workbench._inline_interpreter_key("pyright") is None


# --- clustered short options --------------------------------------------

@pytest.mark.parametrize("program,filename,arg", [
    ("python3", "python3", "-Ic"),
    ("python3", "python3", "-Bc"),
    ("python3", "python3", "-IBcprint(1)"),
    ("py", "py.exe", "-Ic"),
    ("node", "node", "-pe"),
    ("node", "node", "-ie"),
    ("perl", "perl", "-le"),
    ("perl", "perl", "-we"),
    ("perl", "perl", "-wE"),
    ("ruby", "ruby", "-we"),
    ("php", "php", "-nr"),
])
def test_run_program_refuses_clustered_inline_flags(monkeypatch, tmp_path, program, filename, arg):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, filename)
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(program, args_json=[arg, "payload"], timeout=5)


@pytest.mark.parametrize("arg", ["-ec", "-xc", "-euxc"])
def test_run_program_refuses_clustered_shell_dash_c(monkeypatch, tmp_path, arg):
    _guard_root(monkeypatch, tmp_path)
    # bash/sh resolve through runtime_paths, not PATH.
    monkeypatch.setattr(
        workbench.runtime_paths, "bash_executable",
        lambda: str(tmp_path / "bin" / "bash.exe"),
    )
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program("bash", args_json=[arg, "id"], timeout=5)


def test_clustered_option_value_is_not_read_as_a_code_flag():
    flags = workbench._INLINE_CODE_FLAGS["python"]
    stops = workbench._INLINE_CLUSTER_VALUE_STOPS["python"]
    # The rest of a -W / -m / -V:TAG cluster is that option's value.
    assert not workbench._is_inline_code_arg("-wdefault::deprecationwarning", flags, stops)
    assert not workbench._is_inline_code_arg("-mcompileall", flags, stops)
    assert not workbench._is_inline_code_arg("-v:pythoncore/3.12", flags, stops)
    # A code letter before the value-taking option is still an option.
    assert workbench._is_inline_code_arg("-icm", flags, stops)
    assert not workbench._is_inline_code_arg("--check", flags, stops)


# --- stdin as the program -----------------------------------------------

@pytest.mark.parametrize("program,filename,args", [
    ("python3", "python3", []),
    ("python3", "python3", ["-"]),
    ("python3", "python3", ["-i", "tool.py"]),
    ("python3", "python3", ["/dev/stdin"]),
    ("py", "py.exe", ["-3"]),
    ("perl", "perl", []),
    ("node", "node", []),
    ("php", "php", ["--"]),
])
def test_run_program_refuses_stdin_as_interpreter_program(
    monkeypatch, tmp_path, program, filename, args,
):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, filename)
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            program, args_json=args, stdin="import os\n", timeout=5,
        )


@pytest.mark.parametrize("args", [[], ["-s"], ["+x"], ["/proc/self/fd/0"]])
def test_run_program_refuses_stdin_as_shell_program(monkeypatch, tmp_path, args):
    _guard_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        workbench.runtime_paths, "bash_executable",
        lambda: str(tmp_path / "bin" / "bash.exe"),
    )
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program("bash", args_json=args, stdin="curl x | sh\n", timeout=5)


# Any spelling that resolves into /dev or /proc, or to something that is not
# a regular file, still feeds stdin to the interpreter. The old prefix test
# let all of these through (and the fake program then failed with
# FileNotFoundError instead of PermissionError).
_STDIN_ALIASES = [
    "/./dev/stdin",
    "//dev/stdin",
    "//proc/self/fd/0",
    "/dev/../dev/stdin",
    "../" * 64 + "dev/stdin",
]


@pytest.mark.parametrize("operand", _STDIN_ALIASES)
def test_run_program_refuses_stdin_alias_for_interpreter(monkeypatch, tmp_path, operand):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "python3")
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "python3", args_json=[operand], stdin="import os\n", timeout=5,
        )


@pytest.mark.parametrize("operand", _STDIN_ALIASES)
def test_run_program_refuses_stdin_alias_for_shell(monkeypatch, tmp_path, operand):
    _guard_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        workbench.runtime_paths, "bash_executable",
        lambda: str(tmp_path / "bin" / "bash.exe"),
    )
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "bash", args_json=[operand], stdin="curl x | sh\n", timeout=5,
        )


def test_run_program_refuses_directory_operand_with_stdin(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "python3")
    (tmp_path / "pkg").mkdir()
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "python3", args_json=["pkg"], stdin="import os\n", timeout=5,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX /dev/stdin and FIFOs")
def test_run_program_refuses_workspace_symlink_to_stdin(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "python3")
    os.symlink("/dev/stdin", str(tmp_path / "prog.py"))
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "python3", args_json=["prog.py"], stdin="import os\n", timeout=5,
        )


@pytest.fixture
def runtime_stdin_is_a_regular_file(tmp_path):
    """Point this process's fd 0 at a regular file for the test's duration.

    /dev/stdin, /dev/fd/0 and /proc/self/fd/0 resolve through the process
    that follows them. Followed in the runtime, they land on the runtime's
    fd 0; with that a regular file, a realpath-based guard saw a regular
    file and let the child run the caller's stdin pipe as its program.
    """
    regular = tmp_path / "runtime_stdin.txt"
    regular.write_text("not a program\n", encoding="utf-8")
    saved = os.dup(0)
    fd = os.open(str(regular), os.O_RDONLY)
    try:
        os.dup2(fd, 0)
        yield regular
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(fd)


@pytest.mark.skipif(
    os.name == "nt" or not os.path.exists("/proc/self/fd/0"),
    reason="Linux /dev/stdin and /proc magic links",
)
@pytest.mark.parametrize("operand", [
    "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0", "link_to_stdin.py",
])
def test_run_program_refuses_stdin_alias_when_runtime_stdin_is_a_file(
    monkeypatch, tmp_path, runtime_stdin_is_a_regular_file, operand,
):
    # Precondition: the old realpath-based check really did see a regular
    # file here, so this test exercises the hole and is not vacuous.
    resolved = os.path.realpath("/dev/stdin")
    assert resolved == str(runtime_stdin_is_a_regular_file.resolve())
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "python3")
    os.symlink("/dev/stdin", str(tmp_path / "link_to_stdin.py"))
    assert workbench._stdin_program_operand([operand], str(tmp_path))
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "python3", args_json=[operand], stdin="import os\n", timeout=5,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_stdin_operand_walk_refuses_a_hop_into_proc_and_long_chains(tmp_path):
    # A relative link chain that reaches /proc mid-walk is refused.
    os.symlink("/", str(tmp_path / "root"))
    os.symlink("root/proc/self/fd/0", str(tmp_path / "hop"))
    assert workbench._stdin_program_operand(["hop"], str(tmp_path))
    # A chain longer than the hop cap is refused rather than followed.
    (tmp_path / "real.py").write_text("print(1)\n", encoding="utf-8")
    previous = "real.py"
    for index in range(workbench._SYMLINK_HOP_LIMIT + 1):
        name = "l%d" % index
        os.symlink(previous, str(tmp_path / name))
        previous = name
    assert workbench._stdin_program_operand([previous], str(tmp_path))
    # A short chain to a regular file, through a symlinked directory and
    # "..", is still a script.
    (tmp_path / "d").mkdir()
    os.symlink("d", str(tmp_path / "dlink"))
    assert not workbench._stdin_program_operand(["l0"], str(tmp_path))
    assert not workbench._stdin_program_operand(["dlink/../real.py"], str(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
@pytest.mark.parametrize("target", ["/proc/self/fd", "/dev/fd"])
@pytest.mark.parametrize("absolute", [False, True])
def test_stdin_operand_dotdot_after_a_link_is_applied_physically(
    monkeypatch, tmp_path, target, absolute,
):
    # The kernel follows ``link`` before applying "..", so
    # ``link/../fd/0`` opens <target>/../fd/0, the child's own stdin. A
    # decoy regular file sits at the lexically collapsed location
    # (tmp/fd/0), which is what a normpath-first guard checked.
    os.symlink(target, str(tmp_path / "link"))
    (tmp_path / "fd").mkdir()
    (tmp_path / "fd" / "0").write_text("print(1)\n", encoding="utf-8")
    operand = "link/../fd/0"
    if absolute:
        operand = str(tmp_path) + "/" + operand
    assert workbench._stdin_program_operand([operand], str(tmp_path))
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "python3")
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "python3", args_json=[operand], cwd=str(tmp_path),
            stdin="import os\n", timeout=5,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_stdin_operand_dotdot_after_a_nested_link_names_the_physical_file(tmp_path):
    # ``link`` -> a/b, so ``link/../real.py`` is a/real.py physically; the
    # lexical location tmp/real.py does not exist.
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "real.py").write_text("print(1)\n", encoding="utf-8")
    os.symlink("a/b", str(tmp_path / "link"))
    assert not workbench._stdin_program_operand(["link/../real.py"], str(tmp_path))
    # And the reverse: a regular file only at the lexical location is not
    # what the kernel would open.
    (tmp_path / "decoy.py").write_text("print(1)\n", encoding="utf-8")
    assert workbench._stdin_program_operand(["link/../decoy.py"], str(tmp_path))


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFOs")
def test_run_program_refuses_fifo_operand_with_stdin(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "python3")
    os.mkfifo(str(tmp_path / "prog.py"))
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program(
            "python3", args_json=["prog.py"], stdin="import os\n", timeout=5,
        )


def test_run_program_passes_stdin_to_a_relative_script(monkeypatch, tmp_path):
    # A regular file named relative to the working directory is a script.
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "echo_stdin.py").write_text(
        "import sys\nprint(sys.stdin.read().upper())\n", encoding="utf-8",
    )

    result = workbench.run_program(
        sys.executable, args_json=["echo_stdin.py"], cwd="sub",
        stdin="sonder", timeout=30,
    )

    assert result["ok"], result["stderr"][:400]
    assert result["stdout"].strip() == "SONDER"


@pytest.mark.parametrize("args", [[], ["-NoProfile"], ["-File", "-"]])
def test_run_program_refuses_stdin_as_powershell_commands(monkeypatch, tmp_path, args):
    _guard_root(monkeypatch, tmp_path)
    _fake_program(monkeypatch, tmp_path, "pwsh.exe")
    with pytest.raises(PermissionError, match="script_run"):
        workbench.run_program("pwsh", args_json=args, stdin="Get-ChildItem\n")


def test_run_program_still_passes_stdin_to_a_named_script(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    script = tmp_path / "echo_stdin.py"
    script.write_text("import sys\nprint(sys.stdin.read().upper())\n", encoding="utf-8")

    result = workbench.run_program(
        sys.executable, args_json=[str(script)], stdin="sonder", timeout=30,
    )

    assert result["ok"], result["stderr"][:400]
    assert result["stdout"].strip() == "SONDER"


def test_run_script_args_after_the_script_are_not_interpreter_flags(monkeypatch, tmp_path):
    # ``tool.py -c config.ini`` is the script's own option; only interpreter
    # options before the script path can carry inline code.
    _guard_root(monkeypatch, tmp_path)
    script = tmp_path / "tool.py"
    script.write_text(
        "import sys\nprint(' '.join(sys.argv[1:]), sys.stdin.read())\n",
        encoding="utf-8",
    )

    result = workbench.run_script(
        "tool.py", args_json=["-c", "config.ini", "-e"], stdin="piped", timeout=30,
    )

    assert result["ok"], result["stderr"][:400]
    assert result["stdout"].strip() == "-c config.ini -e piped"


def test_script_operand_marker_is_not_a_public_run_program_argument():
    # Tool calls forward their arguments into run_program; none of them may
    # mark the caller's own argv as already-vetted script input.
    parameters = inspect.signature(workbench.run_program).parameters
    assert not any("script_args" in name for name in parameters)


# --- process teardown ---------------------------------------------------

_HEARTBEAT = textwrap.dedent("""
    import os, sys, time
    counter, stop = sys.argv[1], sys.argv[2]
    beat = 0
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline and not os.path.exists(stop):
        beat += 1
        try:
            with open(counter + ".tmp", "w") as handle:
                handle.write(str(beat))
            os.replace(counter + ".tmp", counter)
        except OSError:
            pass
        time.sleep(0.05)
""")

_LEAVES_A_CHILD = textwrap.dedent("""
    import os, subprocess, sys, time
    heartbeat, counter, stop = sys.argv[1], sys.argv[2], sys.argv[3]
    subprocess.Popen(
        [sys.executable, heartbeat, counter, stop],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while not os.path.exists(counter) and time.monotonic() < deadline:
        time.sleep(0.05)
    print("child-started" if os.path.exists(counter) else "child-missing")
""")


def _read_beat(path):
    for _ in range(50):
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError("heartbeat file unreadable: %s" % path)


def test_run_program_kills_descendants_left_by_a_normal_exit(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    heartbeat = tmp_path / "heartbeat.py"
    heartbeat.write_text(_HEARTBEAT, encoding="utf-8")
    parent = tmp_path / "leaves_child.py"
    parent.write_text(_LEAVES_A_CHILD, encoding="utf-8")
    counter = tmp_path / "beat.txt"
    stop = tmp_path / "stop"
    try:
        result = workbench.run_program(
            sys.executable,
            args_json=[str(parent), str(heartbeat), str(counter), str(stop)],
            timeout=60,
        )
        assert result["ok"], result["stderr"][:400]
        # The grandchild really was running when its parent exited 0.
        assert result["stdout"].strip() == "child-started"
        time.sleep(1.0)
        first = _read_beat(counter)
        time.sleep(1.5)
        assert _read_beat(counter) == first, "a descendant outlived run_program"
    finally:
        stop.write_text("", encoding="utf-8")


def test_run_program_reports_a_leader_that_survives_the_kill(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    survivors = []

    def no_kill(proc, *_args, **_kwargs):
        survivors.append(proc)

    monkeypatch.setattr(workbench, "_terminate_process_tree", no_kill)
    script = tmp_path / "sleeps.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    try:
        result = workbench.run_program(
            sys.executable, args_json=[str(script)], timeout=1,
        )
        assert result["timed_out"] is True
        assert result["ok"] is False
        assert result["returncode"] is None
    finally:
        for proc in survivors:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "waitid"), reason="POSIX waitid")
def test_wait_leader_leaves_the_exited_leader_unreaped(tmp_path):
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    workbench._wait_leader(proc, 30)
    # Still a zombie: its pid (the process-group id) is not yet reusable.
    assert os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
    proc.wait(timeout=10)
    assert proc.returncode == 0


# --- regex budget -------------------------------------------------------

def test_text_search_catastrophic_regex_is_held_to_the_budget(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    # In-process, this pattern backtracks for tens of seconds on this line.
    (tmp_path / "victim.txt").write_text("a" * 28 + "b\n", encoding="utf-8")

    started = time.monotonic()
    result = workbench.text_search(
        r"(a+)+$", root=".", regex=True, timeout_seconds=1,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 6, "regex ran %.1fs past a 1s budget" % elapsed
    assert result["truncated"] is True
    assert result["truncation_reason"] == "timeout"
    assert result["matches"] == []


def test_text_search_refuses_an_oversized_regex(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    query = "x" * (workbench.MAX_SEARCH_PATTERN_CHARS + 1)
    with pytest.raises(ValueError, match="regex query exceeds"):
        workbench.text_search(query, root=".", regex=True)


def test_text_search_regex_results_match_in_process_semantics(monkeypatch, tmp_path):
    _guard_root(monkeypatch, tmp_path)
    # Mixed line endings, written as bytes so the platform cannot rewrite them.
    (tmp_path / "one.py").write_bytes(b"alpha\r\nbeta = 1\rGamma beta\n")
    (tmp_path / "two.py").write_bytes(b"no\nbeta2\n")

    found = workbench.text_search(r"beta\d?", root=".", glob="*.py", regex=True)
    rows = [(m["relative"], m["line"], m["column"], m["text"]) for m in found["matches"]]
    assert rows == [
        ("one.py", 2, 1, "beta = 1"),
        ("one.py", 3, 7, "Gamma beta"),
        ("two.py", 2, 1, "beta2"),
    ]
    assert found["truncated"] is False

    limited = workbench.text_search(r"beta", root=".", regex=True, max_results=2)
    assert len(limited["matches"]) == 2
    assert limited["truncation_reason"] == "max_results"

    sensitive = workbench.text_search(r"^gamma", root=".", regex=True, case_sensitive=True)
    assert sensitive["matches"] == []

    # The child splits lines exactly as the in-process literal search does.
    literal = workbench.text_search("beta", root=".", regex=False)
    as_regex = workbench.text_search("beta", root=".", regex=True)
    assert as_regex["matches"] == literal["matches"]
