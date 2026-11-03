"""The recorder subprocess backstop: a hard wall-clock kill, a memory cap, a bounded answer, fail closed."""
from __future__ import annotations

import os
import time

import pytest

from sonder_runtime.adapters.meshgen import record_runner
from sonder_runtime.domain.meshgen.dsl.errors import ScriptError
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

GOOD = "def build():\n    primitive('cube', size=2.0)\n"


def _worker(tmp_path, body):
    path = tmp_path / "fake_worker.py"
    path.write_text("import json, os, sys, time\njob, result = sys.argv[1], sys.argv[2]\n" + body, encoding="utf-8")
    return path


def test_the_real_worker_gives_the_same_recording_as_the_in_process_recorder():
    recording = record_runner.record_script(GOOD, seed=3)
    assert recording.text == compile_script(GOOD, seed=3).text


def test_a_script_error_keeps_its_line_and_message():
    with pytest.raises(ScriptError) as info:
        record_runner.record_script("def build():\n    x = undefined_name\n")
    assert str(info.value).startswith("line 2: ") and "undefined_name" in str(info.value)


def test_a_kit_manifest_is_rebuilt_in_the_child():
    kit = 'def wheel(r):\n    """Make a wheel."""\n'
    recording = record_runner.record_script("import propkit\n\ndef build():\n    propkit.wheel(1)\n",
                                            kit_source=kit, kit_module="propkit")
    assert recording.ops[0]["op"] == "kit.wheel"
    with pytest.raises(ScriptError):
        record_runner.record_script("import propkit\n\ndef build():\n    propkit.nothing(1)\n",
                                    kit_source=kit, kit_module="propkit")


def test_a_worker_that_never_finishes_is_killed_at_the_deadline(tmp_path):
    worker = _worker(tmp_path, "time.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=1.5, worker=worker)
    assert time.monotonic() - started < 10
    assert "time limit of 1.5 s" in str(info.value), str(info.value)


def test_a_busy_loop_in_the_recorder_is_killed_too(tmp_path):
    worker = _worker(tmp_path, "while True:\n    pass\n")
    started = time.monotonic()
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=1.0, worker=worker)
    assert time.monotonic() - started < 10 and "time limit" in str(info.value)


def test_a_worker_that_exhausts_memory_fails_closed(tmp_path):
    worker = _worker(tmp_path, "blocks = []\nfor _ in range(64):\n    blocks.append(bytearray(64 * 1024 * 1024))\n"
                               "open(result, 'w').write(json.dumps({'ok': False, 'line': 1, 'message': 'survived'}))\n")
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=30, memory_mb=256, worker=worker)
    assert "memory limit of 256 MB" in str(info.value), str(info.value)


@pytest.mark.parametrize("body", [
    ("sys.exit(3)\n", "exit code 3"),                                       # crash without an answer
    ("open(result, 'w').write('not json')\n", "could not be read"),
    ("open(result, 'w').write('[1, 2]')\n", "not a JSON object"),
    ("open(result, 'w').write(json.dumps({'ok': True}))\n", "no recording"),
    ("open(result, 'w').write(json.dumps({'ok': True}))\nopen('ops.json', 'w').write('x' * 5000000)\n",
     "larger than 4 MB"),
    ("open(result, 'w').write('0' * 90000)\n", "larger than 64 KB"),                  # oversized answer file
    ("open(result, 'w').write(json.dumps({'ok': True}))\nopen('ops.json', 'w').write('{\"version\": 1}')\n",
     "invalid recording"),
    ("open(result, 'w').write(json.dumps({'ok': False, 'memory': True}))\n", "memory limit"),
])
def test_a_bad_answer_is_a_script_error_with_an_accurate_message(tmp_path, body):
    code, needle = body
    worker = _worker(tmp_path, code)
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=20, worker=worker)
    assert needle in str(info.value), str(info.value)


def test_a_forged_script_error_is_bounded(tmp_path):
    worker = _worker(tmp_path, "open(result, 'w').write(json.dumps({'ok': False, 'line': 'x', 'message': 5}))\n")
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=20, worker=worker)
    assert "malformed" in str(info.value), str(info.value)


def test_the_child_sees_a_minimal_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MESHGEN_CANARY_VALUE", "canary-not-a-secret")
    worker = _worker(tmp_path, "open(result, 'w').write(json.dumps({'ok': False, 'line': 1, "
                               "'message': ','.join(sorted(os.environ))}))\n")
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=20, worker=worker)
    assert "MESHGEN_CANARY_VALUE" not in str(info.value) and "TEMP" in str(info.value)


def test_a_wall_clock_bomb_that_gets_past_the_in_process_accounting_is_still_contained(tmp_path):
    """Stands in for a builtin the work accounting misses: a recorder that simply never returns."""
    worker = _worker(tmp_path, "x = 3 ** (10 ** 8)\n")
    started = time.monotonic()
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=2.0, worker=worker)
    assert time.monotonic() - started < 15 and "time limit" in str(info.value)


def test_a_recording_just_under_the_size_cap_compact_but_over_it_escaped_is_not_refused():
    """4,999 mesh calls with 95 vertices each: 4,173,106 bytes compact, 4,243,106 once escaped (reviewer probe);
    the compact size is what counts.  96 vertices is over the 4 MB cap and is refused."""
    template = "def build():@@    r = [(0, 0, 0)] * %d@@    for i in range(4999):@@        mesh('m', r, [(0, 1, 2)])@@"
    assert len(record_runner.record_script(template.replace("@@", chr(10)) % 95).ops) == 4999
    with pytest.raises(ScriptError):
        record_runner.record_script(template.replace("@@", chr(10)) % 96)


def test_the_memory_cap_holds_on_every_start_not_just_most(tmp_path):
    """The child is assigned to its Job Object just after it starts; it must never get to allocate first."""
    worker = _worker(tmp_path, "blocks = []\nfor _ in range(64):\n    blocks.append(bytearray(64 * 1024 * 1024))\n"
                               "open(result, 'w').write(json.dumps({'ok': False, 'line': 1, 'message': 'SURVIVED'}))\n")
    for _ in range(10):
        with pytest.raises(ScriptError) as info:
            record_runner.record_script(GOOD, timeout=30, memory_mb=256, worker=worker)
        assert "memory limit" in str(info.value), str(info.value)


def test_a_deeply_nested_answer_is_a_script_error_not_a_recursion_crash(tmp_path):
    deep = "[" * 100000 + "]" * 100000
    worker = _worker(tmp_path, "open(result, 'w').write(json.dumps({'ok': True}))\nopen('ops.json', 'w').write(" + repr(deep) + ")\n")
    with pytest.raises(ScriptError):
        record_runner.record_script(GOOD, timeout=20, worker=worker)


@pytest.mark.skipif(os.environ.get("MESHGEN_STRESS") != "1", reason="opt in with MESHGEN_STRESS=1")
def test_the_memory_cap_holds_in_fifty_starts_under_full_cpu_load(tmp_path):
    """Stress for the window between starting the child and putting it in its Job Object."""
    import multiprocessing
    stop = multiprocessing.Event()
    burners = [multiprocessing.Process(target=_burn, args=(stop,)) for _ in range(os.cpu_count() or 4)]
    for burner in burners:
        burner.start()
    worker = _worker(tmp_path, "blocks = []\nfor _ in range(64):\n    blocks.append(bytearray(64 * 1024 * 1024))\n"
                               "open(result, 'w').write(json.dumps({'ok': False, 'line': 1, 'message': 'SURVIVED'}))\n")
    try:
        for _ in range(50):
            with pytest.raises(ScriptError) as info:
                record_runner.record_script(GOOD, timeout=30, memory_mb=256, worker=worker)
            assert "memory limit" in str(info.value), str(info.value)
    finally:
        stop.set()
        for burner in burners:
            burner.join(timeout=30)


def _burn(stop):
    count = 0
    while not stop.is_set():
        count += 1
