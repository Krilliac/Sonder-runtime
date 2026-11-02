"""The recorder subprocess backstop: a hard wall-clock kill, a memory cap, a bounded answer, fail closed."""
from __future__ import annotations

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
    assert record_runner.LIMIT_MESSAGE in str(info.value)


def test_a_busy_loop_in_the_recorder_is_killed_too(tmp_path):
    worker = _worker(tmp_path, "while True:\n    pass\n")
    started = time.monotonic()
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=1.0, worker=worker)
    assert time.monotonic() - started < 10 and record_runner.LIMIT_MESSAGE in str(info.value)


def test_a_worker_that_exhausts_memory_fails_closed(tmp_path):
    worker = _worker(tmp_path, "blocks = []\nfor _ in range(64):\n    blocks.append(bytearray(64 * 1024 * 1024))\n"
                               "open(result, 'w').write(json.dumps({'ok': False, 'line': 1, 'message': 'survived'}))\n")
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=30, memory_mb=256, worker=worker)
    assert record_runner.LIMIT_MESSAGE in str(info.value), str(info.value)


@pytest.mark.parametrize("body", [
    "sys.exit(3)\n",                                                      # crash without an answer
    "open(result, 'w').write('not json')\n",
    "open(result, 'w').write('[1, 2]')\n",
    "open(result, 'w').write(json.dumps({'ok': True}))\n",                 # no text
    "open(result, 'w').write(json.dumps({'ok': True, 'text': 'x' * 5000000}))\n",
    "open(result, 'w').write('0' * 9000000)\n",                            # oversized answer file
    "open(result, 'w').write(json.dumps({'ok': True, 'text': '{\"version\": 1}'}))\n",   # invalid ops document
])
def test_a_bad_answer_is_a_script_error_not_an_exception(tmp_path, body):
    worker = _worker(tmp_path, body)
    with pytest.raises(ScriptError):
        record_runner.record_script(GOOD, timeout=20, worker=worker)


def test_a_forged_script_error_is_bounded(tmp_path):
    worker = _worker(tmp_path, "open(result, 'w').write(json.dumps({'ok': False, 'line': 'x', 'message': 5}))\n")
    with pytest.raises(ScriptError) as info:
        record_runner.record_script(GOOD, timeout=20, worker=worker)
    assert record_runner.LIMIT_MESSAGE in str(info.value)


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
    assert time.monotonic() - started < 15 and record_runner.LIMIT_MESSAGE in str(info.value)
