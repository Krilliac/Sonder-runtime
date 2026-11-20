"""The shell-less reading of a call: run_program's text keys and workspace_run's argv.

The client runs run_program's ``command`` as one argv (``shlex.split``) with no
shell, so ``;``, ``|``, ``&&`` and a newline are arguments of the first program,
and a run flag of that program (``rg --pre``, ``git grep -O``, ``man -P``...)
runs the command it names on every path the program was given, wherever the
path stands.  Each row below is such a call with a root among its paths: BLOCK.

Every command below is classifier input only: it is inspected as text and never run.
"""
from __future__ import annotations

import json
import random
import shlex
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters import restricted_tool_lane as rtl
from sonder_runtime.domain.security import tool_call_classifier as rules

pytestmark = pytest.mark.unit


def _command(code, key="command"):
    return rtl.classifier_verdict("run_program", {key: code})


# These four rows were RED against the relaxation prefix tree (r10/prefix), which let a
# printed ``rm -rf /`` through.  HEAD e5f033b77 already blocks the first three for that
# printed ``rm -rf /``; against HEAD only ``rg -e x / ; echo --hostname-bin=rm`` here, with
# the ``_RUN_FLAG_FORMS`` matrix below, is RED, so those two prove the argv reading.
@pytest.mark.parametrize("code", [
    "rg -e x / ; echo --pre=rm rm -rf /",
    "rg -e x / | echo --pre=rm rm -rf /",
    "rg -e x /\necho --pre=rm rm -rf /",
    "rg -e x / ; echo --hostname-bin=rm",
])
def test_a_run_flag_after_a_separator_runs_on_the_program_s_root(code):
    assert _command(code) == rules.BLOCK


# Each program, a root among its paths, and a run flag naming ``rm``: given whole, as a
# long-flag prefix, with its value as the next word, or in a short-flag bundle.
_RUN_FLAG_FORMS = {
    "rg -e x /": ["--pre=rm", "--pre rm", "--pr=rm", "--hostname-bin=rm", "--hostname-bin rm", "--hostname=rm"],
    "git grep -e x /": ["--open-files-in-pager=rm", "--open-files-in-pager rm", "--open=rm", "-Orm", "-O rm", "-nOrm"],
    "man ls /": ["--pager=rm", "--pager rm", "--pag=rm", "-Prm", "-P rm", "-aPrm", "--html=rm", "-Hrm"],
    "ag x /": ["--pager=rm", "--pager rm", "--pag=rm"],
    "ack x /": ["--pager=rm", "--pager rm", "--pag=rm", "+pager=rm"],
    "printf %s /": ["-vrm", "-v rm"],
}
_SEPARATORS = [" ; ", " | ", " && ", " || ", "\n", " "]


@pytest.mark.parametrize("code", [
    prefix + separator + "echo " + flag
    for prefix, flags in _RUN_FLAG_FORMS.items() for flag in flags for separator in _SEPARATORS
])
def test_every_run_flag_form_blocks_in_run_program_s_command(code):
    assert _command(code) == rules.BLOCK


@pytest.mark.parametrize("key", ["command", "cmd", "script", "stdin"])
@pytest.mark.parametrize("code", ["rg -e x / ; echo --pre rm", "git grep -e x / | echo -Orm", "man ls /\necho -P rm"])
def test_every_text_key_of_run_program_is_read_as_an_argv(key, code):
    assert _command(code, key) == rules.BLOCK


@pytest.mark.parametrize("program, args", [
    ("rg", ["-e", "x", "/", "--pre=rm"]), ("rg", ["-e", "x", "/", "--pre", "rm"]),
    ("rg", ["--pre", "rm", "-e", "x", "/"]), ("git", ["grep", "-Orm", "x", "/"]),
    ("git", ["grep", "-e", "x", "/", "--open-files-in-pager", "rm"]), ("man", ["-P", "rm", "ls", "/"]),
    ("ack", ["--pager=rm", "x", "/"]),
])
def test_a_workspace_run_argv_with_a_run_flag_blocks(program, args):
    assert rtl.classifier_verdict("workspace_run", {"program": program, "args": args}) == rules.BLOCK
    assert rtl.classifier_verdict("workspace_run", {"program": program, "args_json": json.dumps(args)}) == rules.BLOCK


# Every character ``str.strip()`` removes that ``shlex`` does not split on (shlex splits on
# " \t\r\n" only).  The client strips ``command`` before it splits it
# (``executor.py``: ``str(args.get("command") or "").strip()``, then ``shlex.split``), and
# the workbench strips ``program`` (``str(program or "").strip()``), so one of these glued
# to the program name, or to the last word, is gone by the time the argv runs.
_STRIPPED_ONLY = tuple(char for char in map(chr, range(sys.maxunicode + 1))
                       if char.isspace() and char not in " \t\r\n")
_STRIP_ROWS = ["rg -e x / ; echo --pre rm", "git grep -e x / ; echo -O rm", "man ls / ; echo -P rm"]


def _code_point(char):
    return f"U+{ord(char):04X}"


def test_the_stripped_characters_are_the_ones_the_client_strips():
    assert set("\x0b\x0c\x1c\x1d\x1e\x1f\x85\xa0\u2003\u3000") <= set(_STRIPPED_ONLY)
    for char in _STRIPPED_ONLY:
        assert (char + "rg" + char).strip() == "rg"
        assert shlex.split(char + "rg", posix=True) == [char + "rg"]
        assert shlex.split(char + "rg", posix=False) == [char + "rg"]


@pytest.mark.parametrize("where", ["prefix", "suffix"])
@pytest.mark.parametrize("char", _STRIPPED_ONLY, ids=_code_point)
@pytest.mark.parametrize("row", _STRIP_ROWS)
def test_whitespace_the_client_strips_does_not_hide_a_run_flag(row, char, where):
    code = char + row if where == "prefix" else row + char
    assert _command(code) == rules.BLOCK


@pytest.mark.parametrize("char", _STRIPPED_ONLY, ids=_code_point)
@pytest.mark.parametrize("key", ["command", "cmd", "script", "stdin"])
def test_every_text_key_is_read_stripped(key, char):
    assert _command(char + "rg -e x / ; echo --pre rm", key) == rules.BLOCK
    assert _command("git grep -e x / | echo -O rm" + char, key) == rules.BLOCK


@pytest.mark.parametrize("where", ["prefix", "suffix"])
@pytest.mark.parametrize("char", _STRIPPED_ONLY + tuple(" \t\r\n"), ids=_code_point)
@pytest.mark.parametrize("program, args", [
    ("rg", ["-e", "x", "/", "--pre", "rm"]), ("git", ["grep", "-e", "x", "/", "-O", "rm"]),
    ("man", ["-P", "rm", "ls", "/"]),
])
def test_whitespace_around_workspace_run_s_program_does_not_hide_it(program, args, char, where):
    padded = char + program if where == "prefix" else program + char
    assert rtl.classifier_verdict("workspace_run", {"program": padded, "args": args}) == rules.BLOCK
    assert rtl.classifier_verdict("workspace_run", {"program": padded, "args_json": json.dumps(args)}) == rules.BLOCK


@pytest.mark.parametrize("args_key, other_key", [("args", "args_json"), ("args_json", "args")])
def test_each_workspace_run_argument_key_is_read_with_the_program_alone(args_key, other_key):
    # The agent loop runs ``program`` with ``args`` (``args_json`` only without it) and tool
    # dispatch with ``args_json`` (``args`` only without it): each list is the program's.
    reading = rules._floor._argv_readings
    args = ["--pre", "rm", "-e", "x", "/"]
    other = ["-n"]
    arguments = {"program": "\xa0rg", args_key: json.dumps(args) if args_key == "args_json" else args,
                 other_key: json.dumps(other) if other_key == "args_json" else other}
    readings = reading("workspace_run", arguments)
    assert ["rg", *args] in readings
    assert ["rg", *other] in readings
    assert rtl.classifier_verdict("workspace_run", arguments) == rules.BLOCK


@pytest.mark.parametrize("code", [
    "rg -e x src", "rg -e x / --pre=cat", "rg -e x ./src ; echo --pre=rm", "git grep -n x /", "man ls",
    "find / -name rm", "echo --pre=rm", "ls", "echo hi", "\xa0rg -e x src\u3000", "\x0bls -la", "git log\x85",
])
def test_a_harmless_command_does_not_block(code):
    assert _command(code) != rules.BLOCK


@pytest.mark.parametrize("program, args", [("rg", ["-e", "x", "src", "--pre=rm"]), ("rg", ["-e", "x", "/", "--pre=cat"])])
def test_a_harmless_workspace_run_argv_does_not_block(program, args):
    assert rtl.classifier_verdict("workspace_run", {"program": program, "args": args}) != rules.BLOCK


def _executor_argv(command, posix):
    """The argv ``interfaces/client_agent/executor.py`` ``_exec_run_program`` runs for ``command``."""
    tokens = shlex.split(command.strip(), posix=posix)
    return [tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok
            for tok in tokens]


def test_the_linear_split_is_shlex_split():
    split = rules._floor._shlex_words  # the reading must match the client's split
    readings = rules._floor._argv_readings
    chooser = random.Random(20261119)
    alphabet = "ab '\"\\ \t\n;|#=-/" + "".join(_STRIPPED_ONLY[::3]) + "\x0b\xa0\u3000"
    checked = stripped = 0
    for _ in range(4000):
        text = "".join(chooser.choice(alphabet) for _ in range(chooser.randrange(0, 24)))
        for posix in (True, False):
            try:
                expected = shlex.split(text, posix=posix)
            except ValueError:
                continue
            assert split(text, posix) == expected, (text, posix)
            checked += 1
        # The command reading holds the argv the client runs: stripped, then split.
        for posix in (True, False):
            try:
                expected = _executor_argv(text, posix)
            except ValueError:
                continue
            assert expected in readings("run_program", {"command": text}), (text, posix)
            stripped += text.strip() != text
    assert checked > 4000
    assert stripped > 1000


# The desktop client (``app/lib/local_manager_native.dart``, ``executeClientTool``) runs
# ``run_program``'s ``command`` itself: Dart ``String.trim``, then ``_splitCommand``, which
# splits at every character Dart trims (``ch.trim().isEmpty``), removes quotes anywhere, has
# no escape character and keeps a word whose quote is left open; Dart's ``Process.start``
# passes each word up to its first NUL.  These are the characters Dart 3.13.1 trims: Unicode
# White_Space and U+FEFF (``str.isspace`` has \x1c-\x1f, which Dart keeps, and lacks \ufeff).
_DART_BLANKS = tuple(
    "\t\n\x0b\x0c\r \x85\xa0\u1680" + "".join(map(chr, range(0x2000, 0x200B)))
    + "\u2028\u2029\u202f\u205f\u3000\ufeff")


def test_the_floor_splits_at_the_characters_dart_trims():
    assert len(_DART_BLANKS) == 26
    assert set(rules._floor._DART_BLANKS) == set(_DART_BLANKS)
    assert "\ufeff" not in _STRIPPED_ONLY  # Python's strip keeps it; Dart's trim does not


@pytest.mark.parametrize("where", ["prefix", "suffix", "inside"])
@pytest.mark.parametrize("char", _DART_BLANKS, ids=_code_point)
@pytest.mark.parametrize("row", _STRIP_ROWS)
def test_dart_whitespace_does_not_hide_a_run_flag(row, char, where):
    code = {"prefix": char + row, "suffix": row + char, "inside": row.replace(" ", char)}[where]
    assert _command(code) == rules.BLOCK


@pytest.mark.parametrize("code", [
    "\ufeffrg -e x / ; echo --pre rm",
    "\ufeff\ufeffgit grep -e x / ; echo -O rm\ufeff",
    "rg\ufeff-e\ufeffx\ufeff/\ufeff;\ufeffecho\ufeff--pre\ufeffrm",
    "man\ufeffls / ; echo -P\ufeffrm",
    "rg -e x / ;\xa0echo --pre\xa0rm",
    "rg -e x / ; echo --pre\u2003rm",
    "rg -e x / ;\x0becho --pre\x0crm",
    "man ls\xa0/ ; echo -P rm",
])
def test_bom_and_dart_blanks_split_the_command_as_the_desktop_client_does(code):
    assert _command(code) == rules.BLOCK


# A quote left open still runs: its text to the end is the last word.  Each row also has
# a blank (U+FEFF, U+00A0) that, of the argv readings, only the desktop client's
# (``_dart_words``) splits at, so only it sees the root or the run flag, and it must keep
# the open quote's text to see ``rm``.
@pytest.mark.parametrize("code", [
    'rg\ufeff-e x / --pre "rm',
    "rg -e x\ufeff/ --pre 'rm",
    'git grep -e x\ufeff/ -O "rm',
    'rg -e x\xa0"/" --pre r"m',
])
def test_an_unterminated_quote_still_runs_on_the_desktop_client(code):
    assert _command(code) == rules.BLOCK


@pytest.mark.parametrize("code", [
    "rg -e x / --pre rm\x00z",
    "rg -e x /\x00junk --pre rm",
    "rm\x00x -rf /",
    "rg -e x / --pre=rm\x00.exe",
])
def test_each_word_is_cut_at_its_first_nul(code):
    assert _command(code) == rules.BLOCK


def test_the_dart_reading_trims_cwd_path_and_language_as_the_desktop_client_does():
    assert rtl.classifier_verdict("run_program", {"command": "rm -rf .", "cwd": "\ufeff/"}) == rules.BLOCK
    assert rtl.classifier_verdict("file_delete", {"path": "\ufeff/"}) == rules.BLOCK
    assert rtl.classifier_verdict("run_code", {"language": "\ufeffbash", "code": "find ~ -delete"}) == rules.BLOCK


def _dart_split_source():
    """``_splitCommand`` as it stands in the desktop client, as a top-level Dart function."""
    source = (Path(__file__).resolve().parents[1] / "app" / "lib" / "local_manager_native.dart").read_text(
        encoding="utf-8")
    start = source.index("static List<String> _splitCommand(String command) {")
    depth = 0
    for end in range(source.index("{", start), len(source)):
        depth += {"{": 1, "}": -1}.get(source[end], 0)
        if depth == 0:
            return source[start:end + 1].removeprefix("static ")
    raise AssertionError("_splitCommand has no closing brace")


def test_the_dart_reading_equals_the_desktop_client_s_split(tmp_path):
    dart = shutil.which("dart")
    if dart is None:
        pytest.skip("the Dart SDK is not on PATH")
    script = tmp_path / "split.dart"
    script.write_text(
        "import 'dart:convert';\nimport 'dart:io';\n\n" + _dart_split_source() + "\n\n"
        "void main(List<String> args) {\n"
        "  final inputs = (jsonDecode(File(args[0]).readAsStringSync()) as List).cast<String>();\n"
        "  final out = [for (final s in inputs) [for (final t in _splitCommand(s.trim())) t.codeUnits]];\n"
        "  File(args[1]).writeAsStringSync(jsonEncode(out));\n"
        "}\n", encoding="utf-8")
    chooser = random.Random(20261120)
    # Quotes, NUL, a lone surrogate, a non-BMP character, every Dart blank and isspace ones Dart keeps.
    alphabet = "ab'\"\\;|#=-/\x00\ud800\U0001f600\x1c\x1f" + "".join(_DART_BLANKS)
    texts = ["".join(chooser.choice(alphabet) for _ in range(chooser.randrange(0, 24))) for _ in range(5000)]
    inputs, outputs = tmp_path / "in.json", tmp_path / "out.json"
    inputs.write_text(json.dumps(texts), encoding="ascii")
    subprocess.run([dart, "run", str(script), str(inputs), str(outputs)], check=True, timeout=300,
                   capture_output=True)
    results = json.loads(outputs.read_text(encoding="utf-8"))
    assert len(results) == len(texts)
    for text, words in zip(texts, results, strict=True):
        dart_words = [struct.pack(f"<{len(units)}H", *units).decode("utf-16-le", "surrogatepass")
                      for units in words]
        # Dart's Process.start passes each word up to its first NUL (measured on Windows).
        expected = [word.split("\x00", 1)[0] for word in dart_words]
        assert rules._floor._dart_words(text) == expected, repr(text)
        if expected:
            assert expected in rules._floor._argv_readings("run_program", {"command": text}), repr(text)


# find and fd delete (``-delete``) or run a command (find's ``-exec``/``-execdir``/``-ok``/
# ``-okdir``; fd's ``-x``/``-X``/``--exec``/``--exec-batch``) on everything under each path
# they search, wherever that path stands.  GNU find takes its predicates whole (no prefix,
# no bundle: ``--delete`` and ``-delet`` are errors); fd parses with clap, with no
# ``infer_long_args`` (``--exe`` is an error), short flags bundled (``-Hx``) and a value glued
# to its flag (``-xrm``, ``--exec=rm``).  Every command below is classifier input only.
_FINDER_BLOCKS = [
    "find / -delete", "find -L / -delete", "find / -name x -delete", "find . / -delete", "find ~ -delete",
    "find C:\\ -delete", "find /c -delete", "sudo find / -delete", "gfind / -delete", "/usr/bin/find / -delete",
    "find / -del\"ete\"", "find / -exec rm -rf {} +", "find / -exec rm {} ;", "find / -execdir rm {} +",
    "find / -ok rm {} ;", "find / -okdir rm {} +", "find / -exec /bin/rm -rf {} +", "find / -exec sudo rm {} +",
    "find ./x / -exec rm {} +", "find / -type f -exec rm -f {} + -print", "find $HOME -delete",
    "fd . / -x rm -rf", "fd . / -X rm", "fd . / --exec rm", "fd . / --exec-batch rm", "fd . / --exec=rm",
    "fd . / --exec-batch=rm -rf", "fd -x rm . /", "fd -Hxrm . /", "fd -HXrm . ~", "fd -xrm . ~",
    "fd -H -I -x rm -rf . /", "fdfind . / -x rm", "fd -C/ -x rm", "fd --base-directory / -x rm",
    "fd --base-directory=/ -x rm", "fd --search-path / -x rm", "fd --search-path=/ -X rm", "fd . C:\\ -x del",
    "fd -x rm -rf \\; . /",
]
_FINDER_KEEPS = [
    "find ./build -delete", "find . -name x -exec rm {} +", "find . -name '*.pyc' -delete", "find / -name x",
    "find / -exec ls {} +", "find build -type f -delete", "fd . / -x echo", "fd -e py . src -x rm",
    "fd . ./build -X rm -rf", "fd -x rm . ./build",
]


@pytest.mark.parametrize("code", _FINDER_BLOCKS)
def test_a_finder_that_deletes_under_a_root_blocks(code):
    assert _command(code) == rules.BLOCK
    assert rtl.classifier_verdict("run_code", {"language": "bash", "code": code}) == rules.BLOCK


@pytest.mark.parametrize("program, args", [
    ("find", ["/", "-delete"]), ("find", ["/", "-exec", "rm", "-rf", "{}", "+"]),
    ("find", ["/", "-exec", "sh", "-c", 'rm -rf "$1"', "_", "{}", ";"]), ("fd", [".", "/", "-x", "rm"]),
    ("fd", ["-Hxrm", ".", "/"]), ("fd", ["--exec-batch=rm", ".", "C:\\"]), ("find", ["-delete", "/"]),
    ("fd", ["-x", "rm", "-rf", ";", ".", "/"]),
])
def test_a_finder_argv_that_deletes_under_a_root_blocks(program, args):
    assert rtl.classifier_verdict("workspace_run", {"program": program, "args": args}) == rules.BLOCK
    assert rtl.classifier_verdict("workspace_run", {"program": program, "args_json": json.dumps(args)}) == rules.BLOCK


@pytest.mark.parametrize("code", _FINDER_KEEPS)
def test_a_finder_on_a_non_root_path_adds_no_block(code):
    # HEAD's verdict stands: the floor adds nothing.
    assert not rules._floor.blocks("run_program", {"command": code})
    assert not rules._floor.blocks("run_code", {"language": "bash", "code": code})


def test_a_separator_ends_the_finder_in_a_shell():
    assert not rules._floor.blocks("run_code", {"language": "bash", "code": "find . -name x -exec rm {} + ; ls /"})
    assert not rules._floor.blocks("run_code", {"language": "bash", "code": "find ./build -delete\nls /"})


# With no path, GNU find and fd search the working directory (fd: "If omitted, search the
# current working directory"), so a finder delete with a root ``cwd`` deletes under that root.
# Both clients run ``cwd`` unconfined in bypass, and bypass runs an ASK.
@pytest.mark.parametrize("command, cwd", [
    ("find -delete", "/"), ("find -delete", "C:\\"), ("find -delete", "~"), ("find -delete", "C:\\Windows"),
    ("find -delete", "\ufeff/"), ("fd -x rm", "/"), ("fd -Hxrm", "/etc"), ("fd --exec-batch rm", "C:\\"),
])
def test_a_finder_with_no_path_deletes_under_a_root_cwd(command, cwd):
    arguments = {"command": command, "cwd": cwd}
    assert rtl.classifier_verdict("run_program", arguments) == rules.BLOCK
    assert rules.classify("run_program", arguments, risk="safe") == rules.BLOCK


@pytest.mark.parametrize("program, args, cwd", [
    ("find", ["-delete"], "/"), ("fd", ["-x", "rm"], "C:\\"), ("fd", ["-Hxrm"], "/etc"),
])
def test_a_finder_argv_with_no_path_deletes_under_a_root_cwd(program, args, cwd):
    for key, value in (("args", args), ("args_json", json.dumps(args))):
        arguments = {"program": program, key: value, "cwd": cwd}
        assert rtl.classifier_verdict("workspace_run", arguments) == rules.BLOCK, key
        assert rules.classify("workspace_run", arguments, risk="safe") == rules.BLOCK, key


@pytest.mark.parametrize("arguments", [
    {"language": "bash", "code": "cd / && find -delete"}, {"language": "bash", "code": "find -delete", "cwd": "/"},
])
def test_a_finder_with_no_path_deletes_under_a_shell_s_root_working_directory(arguments):
    assert rtl.classifier_verdict("run_code", arguments) == rules.BLOCK
    assert rules.classify("run_code", arguments, risk="safe") == rules.BLOCK


@pytest.mark.parametrize("command, cwd", [
    ("find -delete", None), ("fd -x rm", None), ("find -delete", "./build"), ("find -delete", "build"),
    ("find -delete", "D:\\proj"), ("find -delete", "/home/u/proj"), ("fd -Hxrm", "./build"),
    ("find . -name x -exec rm {} +", "D:\\proj"), ("find . -name x -exec rm {} +", "/home/u/proj"),
])
def test_a_finder_in_a_project_cwd_adds_no_block(command, cwd):
    # HEAD's verdict stands: the floor adds nothing for the working directory of a project.
    where = {} if cwd is None else {"cwd": cwd}
    assert not rules._floor.blocks("run_program", {"command": command, **where})
    assert not rules._floor.blocks("run_code", {"language": "bash", "code": command, **where})
    if cwd is None:
        assert not rules._floor.blocks("workspace_run", {"program": "find", "args": ["-delete"]})
