"""E1: run_program's inline-code refusal must parse argv like the interpreter.

A guard that only matched the inline flag exactly or as a prefix was evaded
by short-option bundles (``python -Ic CODE``, ``bash -ec CODE``, ``perl -we
CODE``), by an option with a value in front of the flag (``python -X dev -c
CODE``), by interpreters missing from its table (``py -c CODE``) and by
program text fed on stdin. It also refused ``python script.py -c``, where the
``-c`` belongs to the script. No interpreter runs here: program resolution and
Popen are stubbed, so a refusal raises PermissionError and an allowed argv
reaches the (fake) launch.
"""

import os

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench


class _Launched(Exception):
    """Raised by the fake Popen: the argv got past the inline guard."""


@pytest.fixture
def launch(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        workbench, "_resolve_program", lambda program, **kwargs: str(program),
    )

    def fake_popen(*args, **kwargs):
        raise _Launched()

    monkeypatch.setattr(workbench.subprocess, "Popen", fake_popen)

    def run(program, args, stdin=""):
        workbench.run_program(program, args_json=args, stdin=stdin, timeout=5)

    return run


PY = "C:/Python312/python.exe"
BASH = "C:/Git/usr/bin/bash.exe"
CODE = "print('INLINE-RAN')"

# (program, argv, stdin) -- each runs inline program text and must be refused.
REFUSED = [
    (PY, ["-c", CODE], ""),
    (PY, ["-Ic", CODE], ""),
    (PY, ["-cprint(1)"], ""),
    (PY, ["-OOc", CODE], ""),
    (PY, ["-E", "-s", "-c", CODE], ""),
    (PY, ["-X", "dev", "-c", CODE], ""),
    (PY, ["-Xdev", "-Ec", CODE], ""),
    (PY, ["-W", "ignore", "-c", CODE], ""),
    (PY, ["--check-hash-based-pycs", "always", "-c", CODE], ""),
    (PY, [], CODE),
    (PY, ["-"], CODE),
    (PY, ["-u"], CODE),
    (PY, ["-i", "script.py"], CODE),
    ("C:/Python312/python3.12.exe", ["-Ic", CODE], ""),
    ("/usr/bin/python3.12", ["-c", CODE], ""),
    ("C:/Python312/PYTHONW.EXE", ["-c", CODE], ""),
    ("C:/Windows/py.exe", ["-c", CODE], ""),
    ("C:/Windows/py.exe", ["-3.12", "-c", CODE], ""),
    ("C:/Windows/py.exe", ["-V:3.12", "-Ic", CODE], ""),
    ("C:/Windows/pyw.exe", ["-c", CODE], ""),
    (BASH, ["-c", "id"], ""),
    (BASH, ["-ec", "id"], ""),
    (BASH, ["-xc", "id"], ""),
    (BASH, ["--norc", "-c", "id"], ""),
    (BASH, ["-o", "pipefail", "-c", "id"], ""),
    (BASH, ["-oc", "pipefail", "id"], ""),
    (BASH, ["+o", "posix", "-c", "id"], ""),
    (BASH, ["-s"], "id"),
    (BASH, [], "id"),
    ("/bin/sh", ["-ec", "id"], ""),
    ("/usr/bin/dash", ["-ec", "id"], ""),
    ("/usr/bin/zsh", ["-fc", "id"], ""),
    ("/usr/bin/perl", ["-e", "print 1"], ""),
    ("/usr/bin/perl", ["-we", "print 1"], ""),
    ("/usr/bin/perl", ["-lne", "print"], ""),
    ("/usr/bin/perl", ["-l0e", "print"], ""),
    ("/usr/bin/perl", ["-E", "say 1"], ""),
    ("/usr/bin/perl", ["-I", "lib", "-e", "print 1"], ""),
    ("/usr/bin/perl", ["-Mstrict;print 1"], ""),
    ("/usr/bin/perl", ["-M", "strict;print 1", "script.pl"], ""),
    ("/usr/bin/perl5.36.0", ["-we", "print 1"], ""),
    ("/usr/bin/perl", [], "print 1"),
    ("/usr/bin/ruby", ["-we", "puts 1"], ""),
    ("/usr/bin/ruby", ["-rjson", "-e", "puts 1"], ""),
    ("/usr/bin/ruby3.2", ["-e", "puts 1"], ""),
    ("/usr/bin/php", ["-nr", "echo 1;"], ""),
    ("/usr/bin/php", ["-R", "echo 1;"], ""),
    ("/usr/bin/php", ["-B", "echo 1;"], ""),
    ("/usr/bin/php8.2", ["-r", "echo 1;"], ""),
    ("C:/node/node.exe", ["-e", "1"], ""),
    ("C:/node/node.exe", ["-pe", "1"], ""),
    ("C:/node/node.exe", ["--eval=1"], ""),
    ("C:/node/node.exe", ["--print", "1"], ""),
    ("C:/node/node.exe", ["-r", "./hook.js", "-e", "1"], ""),
    ("C:/node/node.exe", ["--import", "data:text/javascript,console.log(1)"], ""),
    ("C:/node/node.exe", ["--import=DATA:text/javascript,console.log(1)", "x.js"], ""),
    ("C:/node/node.exe", ["-"], "console.log(1)"),
    ("C:/node/node.exe", [], "console.log(1)"),
    ("C:/deno/deno.exe", ["eval", "console.log(1)"], ""),
    ("C:/deno/deno.exe", ["repl", "--eval", "console.log(1)"], ""),
    ("C:/deno/deno.exe", ["repl", "--eval-file=x.js"], ""),
    ("C:/deno/deno.exe", ["run", "-"], "console.log(1)"),
    ("C:/bun/bun.exe", ["-e", "console.log(1)"], ""),
    ("C:/bun/bun.exe", ["--print", "1"], ""),
]

# (program, argv, stdin) -- ordinary invocations the guard must let through.
ALLOWED = [
    (PY, ["script.py"], ""),
    (PY, ["script.py", "-c", "x"], ""),
    (PY, ["-u", "script.py", "-c", "x"], ""),
    (PY, ["-X", "dev", "script.py"], ""),
    (PY, ["-Xtracemalloc", "script.py"], ""),
    (PY, ["-Wonce", "script.py"], ""),
    (PY, ["--", "-c"], ""),
    (PY, ["-m", "json.tool"], '{"a": 1}'),
    (PY, ["-m", "pkg", "-c", "x"], ""),
    (PY, ["script.py"], "input data"),
    (PY, ["-P", "sealed_main.py", "3", "script.py", "-c"], ""),
    (PY, ["--version"], ""),
    ("C:/Windows/py.exe", ["-3.12", "script.py", "-c"], ""),
    (BASH, ["script.sh", "-c", "x"], ""),
    (BASH, ["-e", "script.sh"], ""),
    (BASH, ["-C", "script.sh"], ""),
    (BASH, ["-o", "pipefail", "script.sh"], ""),
    (BASH, ["/proc/self/fd/5", "-c"], ""),
    ("/usr/bin/perl", ["script.pl", "-e"], ""),
    ("/usr/bin/perl", ["-w", "script.pl"], ""),
    ("/usr/bin/perl", ["-Ilib", "script.pl"], ""),
    ("/usr/bin/perl", ["-MFile::Temp", "script.pl"], ""),
    ("/usr/bin/perl", ["-MList::Util=sum,max", "script.pl"], ""),
    ("/usr/bin/ruby", ["script.rb", "-e"], ""),
    ("/usr/bin/ruby", ["-rset", "script.rb"], ""),
    ("C:/node/node.exe", ["script.js", "-e", "x"], ""),
    ("C:/node/node.exe", ["--import", "./hook.mjs", "script.js"], ""),
    ("C:/node/node.exe", ["script.js"], "input data"),
    ("C:/deno/deno.exe", ["run", "script.ts", "--eval"], ""),
    ("C:/git/git.exe", ["-c", "user.name=x", "status"], ""),
]


def _id(case):
    program, args, stdin = case
    return "%s %s%s" % (program.rsplit("/", 1)[-1], " ".join(args), " <stdin" if stdin else "")


@pytest.mark.parametrize("case", REFUSED, ids=[_id(c) for c in REFUSED])
def test_run_program_refuses_inline_program_text(launch, case):
    program, args, stdin = case
    with pytest.raises(PermissionError, match="script_run"):
        launch(program, args, stdin)


@pytest.mark.parametrize("case", ALLOWED, ids=[_id(c) for c in ALLOWED])
def test_run_program_allows_ordinary_invocations(launch, case):
    program, args, stdin = case
    with pytest.raises(_Launched):
        launch(program, args, stdin)


@pytest.mark.parametrize("basename,key", [
    ("python", "python"),
    ("python3.12.exe", "python"),
    ("pythonw.exe", "python"),
    ("py.exe", "py"),
    ("pyw", "py"),
    ("node.exe", "node"),
    ("perl5.36.0", "perl"),
    ("ruby3.2", "ruby"),
    ("php8.2.exe", "php"),
    ("deno.exe", "deno"),
    ("bun.exe", "bun"),
    ("cat", None),
    ("git.exe", None),
    ("pyright", None),
])
def test_inline_interpreter_key_normalizes_names(basename, key):
    assert workbench._inline_interpreter_key(basename) == key


# Follow-up bypasses: option values and operands that carry program text.
REFUSED_VALUES = [
    # perl -d:MOD=... builds `use Devel::MOD LIST`; bare -d reads debugger
    # commands (code) from stdin; -F's pattern is pasted into split(...).
    ("/usr/bin/perl", ["-d:Foo=1", "script.pl"], ""),
    ("/usr/bin/perl", ["-d=x", "script.pl"], ""),
    ("/usr/bin/perl", ["-d:Foo;print", "script.pl"], ""),
    ("/usr/bin/perl", ["-d", "script.pl"], "p 1"),
    ("/usr/bin/perl", ["-F,", "script.pl"], ""),
    ("/usr/bin/perl", ["-aF/x/", "script.pl"], ""),
    # node folds '_' into '-' in option names, and loads data: URLs.
    ("C:/node/node.exe", ["--experimental_loader", "data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["--experimental_loader=data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["--loader=data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["--require", "data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["-r", "data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["--input_type=module", "--experimental_loader=DATA:x,1"], ""),
    ("C:/node/node.exe", ["--input-type=module"], "console.log(1)"),
    # deno/bun run data: and remote URLs as the program.
    ("C:/deno/deno.exe", ["run", "data:text/javascript,console.log(1)"], ""),
    ("C:/deno/deno.exe", ["run", "https://example.com/x.ts"], ""),
    ("C:/deno/deno.exe", ["https://example.com/x.ts"], ""),
    ("C:/deno/deno.exe", ["test", "http://example.com/x_test.ts"], ""),
    ("C:/deno/deno.exe", ["run", "-A", " DATA:text/javascript,1"], ""),
    ("C:/bun/bun.exe", ["data:text/javascript,console.log(1)"], ""),
    ("C:/bun/bun.exe", ["run", "https://example.com/x.ts"], ""),
    # python -m modules that run code from argv or stdin, or chain to one.
    (PY, ["-m", "code"], ""),
    (PY, ["-m", "timeit", "1"], ""),
    (PY, ["-mtimeit", "1"], ""),
    (PY, ["-Im", "code"], ""),
    (PY, ["-m", "pdb", "script.py"], ""),
    (PY, ["-m", "profile", "-m", "code"], ""),
    (PY, ["-m", "cProfile", "-m", "code"], ""),
    (PY, ["-m", "trace", "--trace", "-m", "code"], ""),
    (PY, ["-m", "runpy", "code"], ""),
    (PY, ["-m", "idlelib", "-c", "1"], ""),
    (PY, ["-m", "idlelib.idle", "-c", "1"], ""),
    (PY, ["-m", "IPython", "-c", "1"], ""),
    (PY, ["-m", "asyncio"], ""),
    (PY, ["-m", "_pyrepl"], ""),
    ("C:/Windows/py.exe", ["-3.12", "-m", "pdb", "script.py"], ""),
]

ALLOWED_VALUES = [
    ("/usr/bin/perl", ["-d:NYTProf", "script.pl"], ""),
    ("/usr/bin/perl", ["-dt:Foo::Bar", "script.pl"], ""),
    ("C:/node/node.exe", ["--require", "./hook.js", "x.js"], ""),
    ("C:/node/node.exe", ["--experimental_loader", "./loader.mjs", "x.js"], ""),
    ("C:/deno/deno.exe", ["run", "main.ts", "https://example.com"], ""),
    ("C:/deno/deno.exe", ["run", "-A", "main.ts"], ""),
    ("C:/bun/bun.exe", ["run", "main.ts"], ""),
    (PY, ["-m", "pytest", "-q"], ""),
    (PY, ["-m", "pip", "--version"], ""),
    (PY, ["-m", "codecs"], ""),
    (PY, ["-m", "compileall", "."], ""),
    (PY, ["-m", "site"], ""),
    (PY, ["-m", "pydoc", "json"], ""),
    (PY, ["-m", "unittest", "discover"], ""),
]


@pytest.mark.parametrize("case", REFUSED_VALUES, ids=[_id(c) for c in REFUSED_VALUES])
def test_run_program_refuses_program_text_in_option_values(launch, case):
    program, args, stdin = case
    with pytest.raises(PermissionError, match="script_run"):
        launch(program, args, stdin)


@pytest.mark.parametrize("case", ALLOWED_VALUES, ids=[_id(c) for c in ALLOWED_VALUES])
def test_run_program_allows_ordinary_option_values(launch, case):
    program, args, stdin = case
    with pytest.raises(_Launched):
        launch(program, args, stdin)


REFUSED_NEW_BLOCKERS = [
    # Unknown-option stdin bypass
    ("C:/node/node.exe", ["--max-old-space-size", "4096"], "console.log(1)"),
    ("C:/node/node.exe", ["--unknown-flag", "val"], "console.log(1)"),
    (PY, ["--unknown-flag", "script.py"], "print(1)"),
    # node --test-reporter=data:
    ("C:/node/node.exe", ["--test-reporter=data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["--test-reporter", "data:text/javascript,1", "x.js"], ""),
    ("C:/node/node.exe", ["--test_reporter=DATA:text/javascript,1"], ""),
    ("C:/node/node.exe", ["--interactive", "x.js"], "console.log(1)"),
    # deno/bun subcommands & unknown options before subcommand
    ("C:/deno/deno.exe", ["--ext", "ts", "eval", "console.log(1)"], ""),
    ("C:/deno/deno.exe", ["--unknown-opt", "val", "eval", "console.log(1)"], ""),
    ("C:/deno/deno.exe", ["run", "--", "data:text/javascript,1"], ""),
    ("C:/bun/bun.exe", ["--cwd", ".", "exec", "console.log(1)"], ""),
    ("C:/bun/bun.exe", ["--unknown-opt", "val", "exec", "console.log(1)"], ""),
    ("C:/bun/bun.exe", ["--preload", "data:text/javascript,1", "x.js"], ""),
    ("C:/bun/bun.exe", ["-r", "data:text/javascript,1", "x.js"], ""),
    ("C:/bun/bun.exe", ["repl"], "console.log(1)"),
    ("C:/bun/bun.exe", ["run", "--", "https://example.com/x.ts"], ""),
    # /dev/stdin and fd/0 variants
    (PY, ["/dev/stdin"], "print(1)"),
    (PY, ["--", "/dev/stdin"], "print(1)"),
    (PY, ["/dev/fd/0"], "print(1)"),
    (PY, ["/proc/self/fd/0"], "print(1)"),
    (BASH, ["/dev/stdin"], "id"),
    ("C:/node/node.exe", ["/dev/stdin"], "console.log(1)"),
    ("C:/deno/deno.exe", ["run", "/dev/stdin"], "console.log(1)"),
    ("/usr/bin/php", ["-f", "/dev/stdin"], "<?php echo 1;"),
    (PY, ["file:///dev/stdin"], "print(1)"),
    # dot-segment spellings of the same stdin operands
    (PY, ["/dev/./stdin"], "print(1)"),
    (PY, ["/dev/../dev/stdin"], "print(1)"),
    (PY, ["/proc/self/fd/../fd/0"], "print(1)"),
    (PY, ["//dev//./stdin"], "print(1)"),
    (BASH, ["/dev/./stdin"], "id"),
    (PY, ["file:///dev/./stdin"], "print(1)"),
    # PowerShell inline guard bypasses
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["/c", "Write-Host 1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["/Command", "Write-Host 1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["\u2013Command", "Write-Host 1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-Command:Write-Host 1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-ec:AA=="], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["Write-Host 1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-NoProfile", "-File", "-"], "Write-Host 1"),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-NoProfile"], "Write-Host 1"),
    ("C:/Program Files/PowerShell/7/pwsh.exe", ["/ec", "AA=="], ""),
    # conhost and other launchers
    ("C:/Windows/System32/conhost.exe", ["--headless", "python", "-c", "print(1)"], ""),
    ("C:/Windows/System32/conhost.exe", ["--width", "80", "powershell.exe", "-Command", "whoami"], ""),
    ("C:/Windows/System32/conhost.exe", ["/headless", "cmd.exe", "/c", "echo hi"], ""),
    ("C:/Windows/System32/conhost.exe", ["--headless", "evil.bat"], ""),
    ("C:/Windows/System32/conhost.exe", ["--unknown-flag", "python", "-c", "print(1)"], ""),
    ("C:/Windows/System32/conhost.exe", ["--headless", "env", "python3", "-Ic", "print(1)"], ""),
    ("/usr/bin/env", ["-i", "FOO=1", "python", "-c", "print(1)"], ""),
    ("/usr/bin/env", ["-S", "python -c 'print(1)'"], ""),
    # env parses -S like getopt: glued values, bundles and long-option
    # prefixes all carry the split string.
    ("/usr/bin/env", ["-Ssh -c id"], ""),
    ("/usr/bin/env", ["-iSsh -c id"], ""),
    ("/usr/bin/env", ["-vS", "sh -c id"], ""),
    ("/usr/bin/env", ["--sp=sh -c id"], ""),
    ("/usr/bin/env", ["--split-str", "sh -c id"], ""),
    ("/usr/bin/env", ["--s=sh -c id"], ""),
    ("/usr/bin/env", ["-i0vSsh -c id"], ""),
    ("/usr/bin/env", ["-uFOO", "-Ssh -c id"], ""),
    ("/usr/bin/env", ["-u", "FOO", "-C", "/tmp", "-S", "sh -c id"], ""),
    # The split words are read again as env options.
    ("/usr/bin/env", ["-S", "-i -u FOO sh -c id"], ""),
    ("/usr/bin/env", ["--split-string=-S 'sh -c id'"], ""),
    # env -S escapes, ${VAR} and comments are not reproduced by shlex.
    ("/usr/bin/env", ["-S", "sh\\_-c\\_id"], ""),
    ("/usr/bin/env", ["-S", "${SHELL} -c id"], ""),
    ("/usr/bin/env", ["-S#x", "sh", "-c", "id"], ""),
    ("/usr/bin/env", ["-S", "sh\n-c\nid"], ""),
    # An unparseable bundle that could carry S is refused.
    ("/usr/bin/env", ["-xSsh -c id"], ""),
    # Optional-argument long options never take the next element.
    ("/usr/bin/env", ["--block-signal", "sh", "-c", "id"], ""),
    ("/usr/bin/env", ["--ignore-signal", "python", "-c", "print(1)"], ""),
    # Assignments after "--" are skipped before the command.
    ("/usr/bin/env", ["--", "FOO=1", "sh", "-c", "id"], ""),
    ("/usr/bin/env", ["-", "FOO=1", "sh", "-c", "id"], ""),
    # An unknown option may consume the next element; later operands and
    # later -S options are still checked.
    ("/usr/bin/env", ["--frobnicate", "x", "sh", "-c", "id"], ""),
    ("/usr/bin/env", ["-x", "x", "-Ssh -c id"], ""),
    ("C:/Windows/System32/wsl.exe", ["-e", "bash", "-c", "id"], ""),
    ("C:/Windows/System32/wsl.exe", [], "id"),
    # wsl without -e/--exec hands the rest of the line to the default shell.
    ("C:/Windows/System32/wsl.exe", ["id; curl x | sh"], ""),
    ("C:/Windows/System32/wsl.exe", ["echo", "$(id)"], ""),
    ("C:/Windows/System32/wsl.exe", ["~", "sh", "-c", "id"], ""),
    ("C:/Windows/System32/wsl.exe", ["~", "id; curl x | sh"], ""),
    ("C:/Windows/System32/wsl.exe", ["-d", "Ubuntu", "ls"], ""),
    ("C:/Windows/System32/wsl.exe", ["--", "ls; id"], ""),
    ("C:/Windows/System32/wsl.exe", ["~", "-e", "sh", "-c", "id"], ""),
    ("/usr/bin/timeout", ["5", "python3", "-c", "print(1)"], ""),
    # GNU timeout with absolute-path arguments is still unwrapped.
    ("/usr/bin/timeout", ["5", "/bin/sh", "-c", "id"], ""),
    ("/usr/bin/timeout", ["5", "/usr/bin/pwsh", "-Command", "x"], ""),
    ("/usr/bin/timeout", ["5", "sh", "-c", "id", "/x"], ""),
    ("/usr/bin/timeout", ["-s", "KILL", "5", "/bin/bash", "-c", "id"], ""),
    ("/usr/bin/nice", ["-n", "5", "ruby", "-e", "puts 1"], ""),
    ("/usr/bin/busybox", ["ash", "-c", "id"], ""),
    # Missing shells
    ("/bin/ash", ["-c", "id"], ""),
    ("/bin/ksh", ["-c", "id"], ""),
    ("/bin/mksh", ["-c", "id"], ""),
    ("/bin/csh", ["-c", "id"], ""),
    ("/bin/tcsh", ["-c", "id"], ""),
    ("/usr/bin/fish", ["-c", "id"], ""),
    ("/usr/bin/fish", ["-C", "id"], ""),
    ("/usr/bin/fish", ["--init-command=id", "script.fish"], ""),
    ("/usr/bin/elvish", ["-c", "echo 1"], ""),
    ("/usr/bin/nu", ["-c", "1"], ""),
    ("/usr/bin/nu", ["--commands=1"], ""),
    ("/usr/bin/nu", ["--stdin", "script.nu"], "1"),
]

ALLOWED_NEW_CASES = [
    ("C:/node/node.exe", ["--max-old-space-size", "4096", "script.js"], ""),
    ("C:/node/node.exe", ["--test-reporter", "spec", "x.test.js"], ""),
    ("C:/deno/deno.exe", ["--allow-net", "run", "script.ts", "eval"], ""),
    ("C:/deno/deno.exe", ["--ext", "ts", "run", "script.ts"], ""),
    ("C:/bun/bun.exe", ["test", "x.test.ts"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "script.ps1"], "input data"),
    ("C:/Windows/System32/conhost.exe", ["--headless", "python", "script.py"], ""),
    ("/usr/bin/env", ["FOO=1", "python", "script.py"], ""),
    ("/usr/bin/env", ["-iSpython script.py -c"], ""),
    ("/usr/bin/env", ["--sp=python script.py", "-v"], ""),
    ("/usr/bin/env", ["-u", "FOO", "--chdir=/tmp", "python", "script.py"], ""),
    ("/usr/bin/env", ["--block-signal=PIPE", "-C", "/tmp", "make"], ""),
    ("/usr/bin/env", ["--", "FOO=1", "python", "script.py"], ""),
    ("/usr/bin/timeout", ["/T", "5"], ""),
    ("/usr/bin/timeout", ["/nobreak"], ""),
    ("/usr/bin/timeout", ["/?"], ""),
    ("/usr/bin/timeout", ["5", "/usr/bin/make", "/x"], ""),
    ("C:/Windows/System32/wsl.exe", ["-e", "ls", "-la"], ""),
    ("C:/Windows/System32/wsl.exe", ["~", "-e", "ls", "/tmp"], ""),
    ("C:/Windows/System32/wsl.exe", ["-d", "Ubuntu", "--exec", "make"], ""),
    ("C:/Windows/System32/wsl.exe", ["--list"], ""),
    ("/usr/bin/fish", ["script.fish"], ""),
]


@pytest.mark.parametrize("case", REFUSED_NEW_BLOCKERS, ids=[_id(c) for c in REFUSED_NEW_BLOCKERS])
def test_run_program_refuses_new_exec_guard_blockers(launch, case):
    program, args, stdin = case
    with pytest.raises(PermissionError, match="script_run"):
        launch(program, args, stdin)


@pytest.mark.parametrize("case", ALLOWED_NEW_CASES, ids=[_id(c) for c in ALLOWED_NEW_CASES])
def test_run_program_allows_legitimate_new_cases(launch, case):
    program, args, stdin = case
    with pytest.raises(_Launched):
        launch(program, args, stdin)


@pytest.mark.skipif(os.name != "nt", reason="System32 timeout.exe is Windows-only")
def test_system32_timeout_exe_is_not_unwrapped(launch, monkeypatch, tmp_path):
    # Windows timeout.exe only waits and never runs a command, so its argv is
    # not unwrapped. A GNU timeout elsewhere with the same argv is.
    root = tmp_path / "Windows"
    monkeypatch.setenv("SystemRoot", str(root))
    args = ["5", "sh", "-c", "id"]
    with pytest.raises(_Launched):
        launch(str(root / "System32" / "timeout.exe"), args)
    with pytest.raises(PermissionError, match="script_run"):
        launch(str(tmp_path / "gnu" / "timeout.exe"), args)


REFUSED_ADVERSARIAL = [
    # bash/sh +c / +lc still execute inline commands
    (BASH, ["+c", "echo 1"], ""),
    (BASH, ["+lc", "echo 1"], ""),
    ("/bin/sh", ["+c", "echo 1"], ""),
    # PowerShell -CommandWithArgs/-cwa, --command, -NoExit stdin, unknown flag before -File,
    # padded stdin operand, inline flag after value switch
    ("C:/Program Files/PowerShell/7/pwsh.exe", ["-cwa", "-File", "run.ps1"], ""),
    ("C:/Program Files/PowerShell/7/pwsh.exe", ["-CommandWithArgs:x", "-File", "run.ps1"], ""),
    ("C:/Program Files/PowerShell/7/pwsh.exe", ["--command", "-File", "run.ps1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-NoExit", "-File", "run.ps1"], "Write-Output 1"),
    ("C:/Program Files/PowerShell/7/pwsh.exe", ["-noe", "-File", "run.ps1"], "Write-Output 1"),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-bogus;Write-Output", "-File", "run.ps1"], ""),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-File", " - "], "Write-Output 1"),
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", ["-WindowStyle", "-c", "-File", "run.ps1"], ""),
    # Windows trailing-dot/space aliases and NTFS ADS suffixes
    ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe.", ["-c", "Write-Output 1"], ""),
    ("C:/Windows/System32/cmd.exe.", ["/c", "echo 1"], ""),
    ("C:/Git/usr/bin/bash.exe.", ["-c", "echo 1"], ""),
    (r"C:\Python312\py.exe::$DATA", ["-c", "print(1)"], ""),
    ("evil.bat.", [], ""),
    # Empty/whitespace operand reads stdin in node/perl/ruby
    ("C:/node/node.exe", [""], "console.log(1)"),
    # GNU timeout `--` before duration must still skip the duration operand
    ("/usr/bin/timeout", ["--", "5", "sh", "-c", "echo 1"], ""),
    # Additional launchers and interpreters
    ("C:/Windows/System32/wslg.exe", ["echo", "1"], ""),
    ("/usr/bin/xargs", ["sh", "-c", "echo 1"], ""),
    ("/usr/bin/lua", ["-e", "print(1)"], ""),
    ("/usr/bin/awk", ["BEGIN{print 1}"], ""),
    ("C:/Windows/System32/forfiles.exe", ["/m", "a", "/c", "cmd"], ""),
    ("C:/Windows/System32/mshta.exe", ["about:blank"], ""),
    ("C:/Windows/System32/pcalua.exe", ["-a", "calc.exe"], ""),
    ("C:/uv/uv.exe", ["run", "python", "-c", "print(1)"], ""),
    ("C:/node/npx.exe", ["node", "-e", "1"], ""),
    # L7-1: npx / pnpx -c / --call inline shell execution
    ("C:/node/npx.exe", ["-c", "echo 1"], ""),
    ("C:/node/npx.exe", ["--call", "echo 1"], ""),
    ("C:/node/npx.exe", ["--call=echo 1"], ""),
    ("C:/node/npx.exe", ["-p", "cowsay", "-c", "echo 1"], ""),
    ("C:/node/pnpx.exe", ["-c", "echo 1"], ""),
    ("C:/node/pnpx.exe", ["--call", "echo 1"], ""),
    # L7-2: sudo -p / -h value handling and -s / -i shell execution
    ("/usr/bin/sudo", ["-p", "prompt", "sh", "-c", "id"], ""),
    ("/usr/bin/sudo", ["-h", "host", "sh", "-c", "id"], ""),
    ("/usr/bin/sudo", ["-s", "echo 1"], ""),
    ("/usr/bin/sudo", ["-i", "echo 1"], ""),
    ("/usr/bin/sudo", ["--shell", "echo 1"], ""),
    ("/usr/bin/sudo", ["--login", "echo 1"], ""),
    ("/usr/bin/sudo", ["-u", "root", "-s", "id"], ""),
    # L7-3: awk / gawk -F -f, -v -f, stdin -f/-i, --sour, -Pe, -Wsource=
    ("/usr/bin/awk", ["-F", "-f", "BEGIN{print 1}"], ""),
    ("/usr/bin/awk", ["-v", "-f", "BEGIN{print 1}"], ""),
    ("/usr/bin/gawk", ["-f", "-"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["-f-"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["--file=-"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["--file", "-"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["-f", "/dev/stdin"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["-f", "/proc/self/fd/0"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["-i", "/dev/stdin", "-f", "/dev/null"], "BEGIN{print 1}"),
    ("/usr/bin/gawk", ["-f", "a.awk", "--sour", "BEGIN{print 1}"], ""),
    ("/usr/bin/gawk", ["-f", "a.awk", "-Pe", "BEGIN{print 1}"], ""),
    ("/usr/bin/gawk", ["-f", "a.awk", "-Wsource=BEGIN{print 1}"], ""),
    ("/usr/bin/gawk", ["-f", "a.awk", "-W", "source=BEGIN{print 1}"], ""),
    # L7-4: bun x launcher alias
    ("C:/bun/bun.exe", ["x", "sh", "-c", "id"], ""),
    ("C:/bun/bun.exe", ["x", "--bun", "sh", "-c", "id"], ""),
    # L7-5: uv run - / --script -, uv tool run, uvx
    ("C:/uv/uv.exe", ["run", "-"], "print(1)"),
    ("C:/uv/uv.exe", ["run", "--script", "-"], "print(1)"),
    ("C:/uv/uv.exe", ["run", "--python", "3.12", "-"], "print(1)"),
    ("C:/uv/uv.exe", ["tool", "run", "python", "-c", "1"], ""),
    ("C:/uv/uvx.exe", ["python", "-c", "1"], ""),
    # L7-6: php -d / --define auto_prepend_file / auto_append_file stream wrappers
    ("C:/php/php.exe", ["-d", "auto_prepend_file=data://text/plain,<?php echo 1;", "script.php"], ""),
    ("C:/php/php.exe", ["-d", "auto_prepend_file=php://input", "script.php"], "<?php echo 1;"),
    ("C:/php/php.exe", ["-dauto_prepend_file=php://input", "script.php"], "<?php echo 1;"),
    ("C:/php/php.exe", ["--define", "auto_prepend_file=data://text/plain,<?php echo 1;", "script.php"], ""),
    ("C:/php/php.exe", ["--define=auto_append_file=php://filter/resource=x", "script.php"], ""),
    # L7-7: git-bash and pypy / pypy3 aliases
    ("C:/Program Files/Git/git-bash.exe", ["-c", "id"], ""),
    ("/usr/bin/git-bash", ["-c", "id"], ""),
    ("/usr/bin/pypy3", ["-c", "1"], ""),
    ("/usr/bin/pypy", ["-c", "1"], ""),
    # Hardening: bash --rcfile / --init-file reading stdin
    ("/usr/bin/bash", ["--rcfile", "/dev/stdin", "script.sh"], "id"),
    ("/usr/bin/bash", ["--init-file", "-", "script.sh"], "id"),
]

ALLOWED_ADVERSARIAL = [
    ("C:/uv/uv.exe", ["run", "python", "script.py"], ""),
    ("C:/uv/uv.exe", ["run", "pytest"], ""),
    ("C:/uv/uv.exe", ["--version"], ""),
    ("C:/node/npx.exe", ["eslint", "src/"], ""),
    ("/usr/bin/sudo", ["-u", "root", "ls"], ""),
    ("C:/bun/bun.exe", ["run", "build"], ""),
    ("/usr/bin/lua", ["script.lua"], ""),
    ("/usr/bin/awk", ["-f", "prog.awk", "data.txt"], ""),
    ("/usr/bin/gawk", ["-F", ",", "-f", "script.awk", "in.txt"], ""),
]


@pytest.mark.parametrize("case", REFUSED_ADVERSARIAL, ids=[_id(c) for c in REFUSED_ADVERSARIAL])
def test_run_program_refuses_adversarial_bypasses(launch, case):
    program, args, stdin = case
    with pytest.raises(PermissionError, match="script_run"):
        launch(program, args, stdin)


@pytest.mark.parametrize("case", ALLOWED_ADVERSARIAL, ids=[_id(c) for c in ALLOWED_ADVERSARIAL])
def test_run_program_allows_adversarial_benign_controls(launch, case):
    program, args, stdin = case
    with pytest.raises(_Launched):
        launch(program, args, stdin)


def test_resolve_program_refuses_network_path_before_realpath(monkeypatch):
    hits = []
    monkeypatch.setattr(workbench.os.path, "realpath", lambda p: hits.append(p) or p)
    with pytest.raises(PermissionError):
        workbench._resolve_program(r"\\attacker\share\evil.exe")
    assert hits == []


def test_run_program_abandons_child_and_closes_tree_on_keyboard_interrupt(monkeypatch, tmp_path):
    events = []

    class FakePipe:
        def read(self, *_args):
            return b""

        def write(self, *_args):
            return 0

        def close(self):
            events.append("close_pipe")

    class FakeProc:
        pid = 9999
        returncode = None

        def __init__(self):
            self.stdin = FakePipe()
            self.stdout = FakePipe()
            self.stderr = FakePipe()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if not events or "interrupt" not in events:
                events.append("interrupt")
                raise KeyboardInterrupt()
            self.returncode = -9
            events.append("reaped")
            return self.returncode

        def kill(self):
            events.append("kill")
            self.returncode = -9

    class FakeTree:
        def terminate(self, **_kwargs):
            events.append("tree_terminate")

        def close(self):
            events.append("tree_close")

        def release(self):
            events.append("tree_release")

    monkeypatch.setattr(workbench, "_resolve_program", lambda *_a, **_kw: "fake-prog")
    monkeypatch.setattr(workbench, "_check_program_guards", lambda *_a, **_kw: None)
    monkeypatch.setattr(workbench, "_resolve", lambda *_a, **_kw: tmp_path)
    monkeypatch.setattr(workbench.subprocess, "Popen", lambda *_a, **_kw: FakeProc())
    monkeypatch.setattr(workbench.process_tree, "is_real_popen", lambda _p: True)
    monkeypatch.setattr(workbench.process_tree.ProcessTree, "owning", staticmethod(lambda _p: FakeTree()))

    with pytest.raises(KeyboardInterrupt):
        workbench.run_program("fake-prog", cwd=str(tmp_path), timeout=5)

    assert "tree_terminate" in events
    assert "tree_close" in events
    assert "tree_release" not in events


# --- E1-E4 residual L7 guard regression tests (#100 review) ------------------


@pytest.mark.parametrize(
    ("program", "args"),
    [
        ("sudo", ["-Hs", "echo pwn"]),
        ("sudo", ["-Hi", "echo pwn"]),
        ("sudo", ["-As", "echo pwn"]),
        ("sudo", ["-Ps", "echo pwn"]),
        ("sudo", ["-nHs", "echo pwn"]),
        ("sudo", ["FOO=bar", "sh", "-c", "id"]),
        ("sudo", ["-u", "root", "FOO=bar", "sh", "-c", "id"]),
        ("sudo", ["--", "FOO=bar", "sh", "-c", "id"]),
        ("uv", ["run", "-m", "timeit", "1"]),
        ("uv", ["run", "--module", "timeit", "1"]),
        ("uv", ["run", "--module=timeit", "1"]),
        ("uv", ["run", "-mtimeit", "1"]),
        ("uv", ["run", "-m", "code"]),
        ("uv", ["run", "-m", "pdb", "s.py"]),
        ("uv", ["run", "-m", "runpy", "s"]),
        ("uv", ["tool", "-q", "run", "python", "-c", "1"]),
        ("uv", ["tool", "--offline", "run", "python", "-c", "1"]),
        ("php", ["-d", 'auto_prepend_file=" data://text/plain,<?php echo 1;"', "s.php"]),
        ("php", ["--define", 'auto_append_file=" data://text/plain,<?php echo 1;"', "s.php"]),
    ],
)
def test_e1_to_e3_residual_bypasses_are_refused(program, args):
    with pytest.raises(PermissionError):
        workbench._check_program_guards(program, args)


@pytest.mark.parametrize(
    ("program", "args", "stdin"),
    [
        ("php", ["-d", 'auto_prepend_file=" php://input "', "s.php"], "<?php echo 1;\n"),
        ("gawk", ["-f", "a.awk", "--inc=-"], "BEGIN{print 1}\n"),
        ("gawk", ["-f", "a.awk", "--fi=-"], "BEGIN{print 1}\n"),
        ("gawk", ["-f", "a.awk", "--ex=-"], "BEGIN{print 1}\n"),
        ("gawk", ["-f", "a.awk", "-W", "inc=-"], "BEGIN{print 1}\n"),
        ("gawk", ["-f", "a.awk", "-W", "fi=-"], "BEGIN{print 1}\n"),
        ("gawk", ["-f", "a.awk", "-W", "ex=-"], "BEGIN{print 1}\n"),
    ],
)
def test_e3_e4_stdin_bypasses_are_refused(program, args, stdin):
    with pytest.raises(PermissionError):
        workbench._check_program_guards(program, args, stdin=stdin)


@pytest.mark.parametrize(
    ("program", "args"),
    [
        ("sudo", ["-S", "ls"]),
        ("uv", ["run", "-m", "pytest"]),
        ("uv", ["run", "--module", "pytest", "-q"]),
        ("gawk", ["--field-separator", ",", "-f", "a.awk"]),
    ],
)
def test_e1_to_e4_benign_controls_remain_allowed(program, args):
    workbench._check_program_guards(program, args)
