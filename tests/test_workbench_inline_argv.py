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
