"""Tail retention regressions for bounded process output."""
import io
import json
import sys

import pytest

from sonder_runtime.adapters.filesystem import file_ops, workbench
from sonder_runtime.adapters.observability.run_result_formatting import format_run_result


@pytest.mark.parametrize("limit", [1, 2, 9, 128_000])
def test_drain_pipe_retains_head_and_last_bytes(limit):
    payload = b"START\n" + b"x" * 300_000 + b"\nLAST LINE\n"
    sink, state = bytearray(), {"bytes": 0}
    pipe = io.BytesIO(payload)
    workbench._drain_pipe(pipe, sink, state, limit)
    head = limit // 2
    assert sink == payload[:head] + payload[-(limit - head):]
    assert state["bytes"] == len(payload)
    assert pipe.closed


@pytest.mark.parametrize("size", [0, 200, 63_999, 64_000, 64_001, 127_999, 128_000])
def test_drain_pipe_small_output_is_lossless(size):
    payload = bytes(range(256)) * (size // 256) + b"z" * (size % 256)
    sink, state = bytearray(), {"bytes": 0}
    workbench._drain_pipe(io.BytesIO(payload), sink, state, 128_000)
    assert sink == payload
    assert state["bytes"] == size


def test_300kb_script_failure_survives_capture_and_model_view(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    script = tmp_path / "failure.py"
    script.write_text(
        "import sys\nsys.stdout.write('x' * 300000 + "
        "'\\nFAILED tests/x.py::t - AssertionError\\n1 failed\\n')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    data = workbench.run_program(sys.executable, args_json=json.dumps([str(script)]), cwd=str(tmp_path))
    rendered = format_run_result("workspace run", data, digest=True)
    assert rendered.splitlines()[0].startswith("exit 1 (")
    assert "FAILED tests/x.py::t - AssertionError" in rendered
    assert "1 failed" in rendered
    assert len(rendered) <= 6000
