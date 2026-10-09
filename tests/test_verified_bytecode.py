"""Owner-compiled bytecode for the managed child: reused only for identical source."""

import json
import marshal
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.runtime_payload import compile_bytecode

REPO = Path(__file__).resolve().parents[1]

CHILD = """
import json, sys
sys.path.insert(0, {source!r}); sys.path.append({repo!r})
import sonder_runtime.bootstrap.verified_bytecode as bytecode
count = bytecode.install(json.loads(sys.argv[1])) if {install} else 0
import mod, other
print(json.dumps({{"count": count, "mod": mod.value, "other": other.value,
                  "stats": bytecode.stats}}))
"""


def _bundle(tmp_path, source):
    data, _ = compile_bytecode(((str(source), False),))
    path = tmp_path / "python-bytecode.marshal"
    path.write_bytes(data)
    return [str(path), sha256(data).hexdigest()]


def _run(tmp_path, source, binding, *, install=True, flags=("-B",)):
    prefix = tmp_path / "python-cache"
    prefix.mkdir(exist_ok=True)
    code = CHILD.format(source=str(source), repo=str(REPO), install=install)
    return subprocess.run(
        [sys.executable, "-E", "-S", *flags, "-X", "pycache_prefix=" + str(prefix),
         "-c", code, json.dumps(binding)],
        capture_output=True, text=True, timeout=60, cwd=tmp_path)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (root / "other.py").write_text("value = 'source'\n", encoding="utf-8")
    return root


def test_compiled_bytecode_is_served_and_nothing_is_written(tmp_path, source):
    result = _run(tmp_path, source, _bundle(tmp_path, source))
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["count"] == 2 and value["mod"] == 1
    assert value["stats"]["served"] == 2
    assert not any((tmp_path / "python-cache").rglob("*")), "the child must never write bytecode"


def test_changed_source_with_identical_size_and_mtime_never_reuses_bytecode(tmp_path, source):
    # The stale-bytecode shape test_selfmod guards against: same size, same
    # mtime, different bytes. A timestamp pyc would run the old code.
    binding = _bundle(tmp_path, source)
    target = source / "mod.py"
    stamp = target.stat().st_mtime_ns
    target.write_text("value = 2\n", encoding="utf-8")
    os.utime(target, ns=(stamp, stamp))
    result = _run(tmp_path, source, binding)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["mod"] == 2
    assert value["stats"]["served"] == 2  # offered, then rejected by the source hash check


def test_bytecode_that_differs_from_the_manifest_digest_is_refused(tmp_path, source):
    path, digest = _bundle(tmp_path, source)
    data = bytearray(Path(path).read_bytes())
    data[-1] ^= 1
    Path(path).write_bytes(bytes(data))
    result = _run(tmp_path, source, [path, digest])
    assert result.returncode != 0
    assert "VerifiedBytecodeError" in result.stderr
    assert "differs from the payload manifest" in result.stderr


def test_bytecode_planted_under_the_private_prefix_is_never_read(tmp_path, source):
    from importlib._bootstrap_external import _code_to_hash_pyc
    from importlib.util import cache_from_source, source_hash

    # Leave other.py out of the verified bundle: a planted unchecked pyc for
    # it would be honoured by the stock import system.
    binding = _bundle(tmp_path, source)
    table = marshal.loads(Path(binding[0]).read_bytes())
    table.pop(str(source / "other.py"))
    raw = marshal.dumps(table)
    Path(binding[0]).write_bytes(raw)
    binding[1] = sha256(raw).hexdigest()
    prefix = tmp_path / "python-cache"
    planted_source = (source / "other.py").read_bytes()
    planted = _code_to_hash_pyc(compile("value = 'planted'\n", str(source / "other.py"), "exec"),
                                source_hash(planted_source), False)
    previous = sys.pycache_prefix
    sys.pycache_prefix = str(prefix)
    try:
        location = Path(cache_from_source(str(source / "other.py")))
    finally:
        sys.pycache_prefix = previous
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_bytes(bytes(planted))
    without = _run(tmp_path, source, None, install=False)
    assert without.returncode == 0, without.stderr
    assert json.loads(without.stdout)["other"] == "planted", "control: stock import reads it"

    result = _run(tmp_path, source, binding)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["other"] == "source"
    assert value["stats"]["refused"] >= 1


def test_install_refuses_an_interpreter_that_may_write_bytecode(tmp_path, source):
    result = _run(tmp_path, source, _bundle(tmp_path, source), flags=())
    assert result.returncode != 0
    assert "requires -B" in result.stderr


def test_no_binding_leaves_the_import_system_unchanged(tmp_path, source):
    result = _run(tmp_path, source, None)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["count"] == 0 and value["stats"] == {"served": 0, "refused": 0}


def test_compile_bytecode_keys_by_source_path_and_skips_tests_and_bad_syntax(tmp_path):
    root = tmp_path / "root"
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "good.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "bad.py").write_text("def (:\n", encoding="utf-8")
    (root / "pkg" / "tests" / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "data.txt").write_text("not python\n", encoding="utf-8")
    data, digests = compile_bytecode(((str(root), False),))
    table = marshal.loads(data)
    assert digests[str(root / "pkg" / "good.py")] == sha256(
        (root / "pkg" / "good.py").read_bytes()).hexdigest()
    assert sorted(table) == sorted([str(root / "pkg" / "__init__.py"), str(root / "pkg" / "good.py")])
    from importlib.util import source_hash
    pyc = table[str(root / "pkg" / "good.py")]
    assert int.from_bytes(pyc[4:8], "little") == 0b11  # hash-based and checked
    assert pyc[8:16] == source_hash((root / "pkg" / "good.py").read_bytes())


def test_bytecode_must_be_compiled_from_the_hashed_sources(tmp_path):
    from sonder_runtime.adapters.execution.runtime_payload import _require_compiled_from
    from sonder_runtime.application.ports.runtime_owner import OwnerRefused

    site = tmp_path / "resolved" / "site-packages"
    alias = tmp_path / "venv" / "Lib" / "site-packages"
    rows = [[str(alias / "dep.py"), 0, 0, 1, "a" * 64], [str(tmp_path / "mod.py"), 0, 0, 1, "b" * 64]]
    _require_compiled_from({str(site / "dep.py"): "a" * 64, str(tmp_path / "mod.py"): "b" * 64},
                           rows, site, alias)
    with pytest.raises(OwnerRefused, match="changed during inspection"):
        _require_compiled_from({str(tmp_path / "mod.py"): "c" * 64}, rows, site, alias)
    with pytest.raises(OwnerRefused, match="changed during inspection"):
        _require_compiled_from({str(tmp_path / "unhashed.py"): "b" * 64}, rows, site, alias)
