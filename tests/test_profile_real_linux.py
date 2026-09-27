"""Real profilers on this host against a seeded C++ program.

Each test is skipped when its tool is absent (or, for perf, when
perf_event_open is not permitted). Processes are launched by the test with a
bounded timeout; the readers under test only ever see the produced text.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.domain.profiling.callgrind import parse_callgrind
from sonder_runtime.domain.profiling.heaptrack_text import parse_heaptrack_print
from sonder_runtime.domain.profiling.perf_text import (
    merge_perf_digests,
    parse_perf_flat,
    parse_perf_folded,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux host tools")

FIXTURES = Path(__file__).parent / "fixtures" / "profiling"
HOT_FUNCTION = "integrate_physics(int)"


def _run(argv, cwd, timeout=180, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    merged.pop("DEBUGINFOD_URLS", None)
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=merged)


def _build(tmp_path: Path, source: str, *flags: str) -> Path:
    if shutil.which("g++") is None:
        pytest.skip("g++ not installed")
    shutil.copy(FIXTURES / source, tmp_path / source)
    binary = tmp_path / Path(source).stem
    done = _run(["g++", "-O1", "-g", *flags, source, "-o", binary.name], tmp_path)
    assert done.returncode == 0, done.stderr
    return binary


def test_callgrind_top_self_matches_callgrind_annotate(tmp_path):
    if shutil.which("valgrind") is None or shutil.which("callgrind_annotate") is None:
        pytest.skip("valgrind/callgrind_annotate not installed")
    binary = _build(tmp_path, "hot.cpp")
    out = tmp_path / "callgrind.out.test"
    done = _run(["valgrind", "--tool=callgrind", "--callgrind-out-file=%s" % out,
                 "./%s" % binary.name, "300000"], tmp_path)
    assert done.returncode == 0, done.stderr[-2000:]
    with out.open(encoding="utf-8", errors="replace") as handle:
        digest = parse_callgrind(handle)
    assert digest.top_self[0].name == HOT_FUNCTION
    annotate = _run(["callgrind_annotate", str(out)], tmp_path).stdout
    totals = re.search(r"^\s*([\d,]+) \(100\.0%\)\s+PROGRAM TOTALS", annotate, re.MULTILINE)
    hot = re.search(r"^\s*([\d,]+) \(\s*[\d.]+%\)\s+\S*:" + re.escape(HOT_FUNCTION),
                    annotate, re.MULTILINE)
    assert totals and hot, annotate[:2000]
    program_total = int(totals.group(1).replace(",", ""))
    hot_self = int(hot.group(1).replace(",", ""))
    ours_total = max(fn.total_value or 0 for fn in digest.top_total)
    assert abs(digest.top_self[0].self_value - hot_self) <= hot_self * 0.01
    assert abs(ours_total - program_total) <= program_total * 0.01
    assert not any("self costs sum" in note for note in digest.notes)  # self sum == header total


def _perf_binary() -> str | None:
    candidates = sorted(glob.glob("/usr/lib/linux-tools*/*/perf") + glob.glob("/usr/lib/linux-tools-*/perf"))
    for candidate in reversed(candidates):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def test_perf_record_and_report_templates(tmp_path):
    perf = _perf_binary()
    if perf is None:
        pytest.skip("versioned perf binary not installed")
    binary = _build(tmp_path, "hot.cpp", "-fno-omit-frame-pointer")
    env = {"PERF_CONFIG": "/dev/null", "PERF_BUILDID_DIR": str(tmp_path / "buildid")}
    record = _run([perf, "record", "-e", "cpu-clock", "-g", "-o", "perf.data",
                   "./%s" % binary.name, "20000000"], tmp_path, env=env)
    if record.returncode != 0 or not (tmp_path / "perf.data").exists():
        pytest.skip("perf_event_open not permitted: %s" % record.stderr[-300:])
    folded = _run([perf, "report", "-i", "perf.data", "--stdio", "--no-children", "--percent-limit",
                   "0.5", "--max-stack", "32", "-g", "folded,0.5,caller,function,percent",
                   "--sort", "dso,sym"], tmp_path, env=env)
    flat = _run([perf, "report", "-i", "perf.data", "--stdio", "--children", "-g", "none",
                 "--percent-limit", "0.3", "--sort", "dso,sym"], tmp_path, env=env)
    assert folded.returncode == 0 and flat.returncode == 0, folded.stderr + flat.stderr
    digest = merge_perf_digests(parse_perf_folded(folded.stdout), parse_perf_flat(flat.stdout))
    assert digest.metadata.event == "cpu-clock" and (digest.metadata.sample_count or 0) > 50
    assert digest.top_self[0].name == HOT_FUNCTION
    assert digest.top_self[0].self_pct > 50.0
    assert any(path.frames[-1] == HOT_FUNCTION for path in digest.hot_paths)


def test_heaptrack_leak_site_and_allocator_hotspot(tmp_path):
    if shutil.which("heaptrack") is None or shutil.which("heaptrack_print") is None:
        pytest.skip("heaptrack not installed (apt-get install -y heaptrack)")
    binary = _build(tmp_path, "leak.cpp")
    done = _run(["heaptrack", "-o", str(tmp_path / "capture"), "./%s" % binary.name], tmp_path)
    assert done.returncode == 0, done.stderr[-2000:]
    captures = sorted(tmp_path.glob("capture*"))
    assert captures, done.stdout[-2000:]
    printed = _run(["heaptrack_print", str(captures[0]), "--print-peaks", "1",
                    "--print-allocators", "1", "--print-leaks", "1", "--print-temporary", "1",
                    "--peak-limit", "20"], tmp_path)
    assert printed.returncode == 0, printed.stderr[-2000:]
    digest = parse_heaptrack_print(printed.stdout)
    by_function = {hot.function: hot for hot in digest.allocations}
    leak = by_function.get("leak_buffer(unsigned long)")
    assert leak is not None and leak.leaked_bytes == 40_960 and leak.allocations == 10
    assert leak.file and leak.file.endswith("leak.cpp")
    churn = by_function.get("churn()")
    assert churn is not None and churn.allocations == 2000


def test_heaptrack_print_flags_match_help():
    if shutil.which("heaptrack_print") is None:
        pytest.skip("heaptrack_print not installed")
    help_text = _run(["heaptrack_print", "--help"], "/").stdout
    for flag in ("--print-peaks", "--print-allocators", "--print-leaks", "--print-temporary",
                 "--peak-limit"):
        assert flag in help_text
