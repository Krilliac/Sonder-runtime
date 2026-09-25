"""report.py: the 48 KB wire fit, rendering and path relabeling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonder_runtime.domain.build.attribution import attribute_steps, first_errors
from sonder_runtime.domain.build.model import BuildDomainError
from sonder_runtime.domain.build.output import parse_build_diagnostics, parse_dash_H, parse_ninja_segments
from sonder_runtime.domain.build.report import (
    build_report_to_wire,
    make_build_report,
    relabel_attribution,
    relabel_trace,
    render_build_report,
    scrub_paths,
)
from sonder_runtime.domain.diagnostics.digest import digest_text
from sonder_runtime.domain.diagnostics.model import make_diagnostic

LOGS = Path(__file__).parent / "fixtures" / "cpp_build" / "logs"
SOURCE = "/work/sparklite"
BUILD = SOURCE + "/build/ninja-debug"


def _report(**overrides):
    text = (LOGS / "ninja-gxx.log").read_text()
    atts = attribute_steps(text, parse_build_diagnostics(text), parse_ninja_segments(text))
    atts = tuple(relabel_attribution(item, source_root=SOURCE, build_dir=BUILD) for item in atts)
    fields = dict(
        status="failed", job_id="build-job-" + "a" * 16, action="build", system="cmake",
        target="game", config="Debug", command_digest="d" * 64,
        display_command="cmake --build <build> --target game --parallel 4",
        world="host", network="enforced_off", isolation_truth="unverified", exit_code=1,
        duration_seconds=3.5, attributions=atts, counts=(("error", 2),),
        first_errors=first_errors(atts),
        output_digest=digest_text(scrub_paths(text, source_root=SOURCE, build_dir=BUILD)).to_wire(),
    )
    fields.update(overrides)
    return make_build_report(**fields)


def test_report_wire_and_render_have_no_absolute_source_paths():
    report = _report()
    wire = build_report_to_wire(report)
    assert "/work/" not in json.dumps(wire)
    assert [item["label"] for item in wire["attributions"]] == ["src/game/main.cpp", "src/core/math.cpp"]
    assert wire["first_errors"][0]["file"] == "src/game/main.cpp"
    rendered = render_build_report(report)
    assert "build build game: failed (exit 1)" in rendered and "src/core/math.cpp" in rendered
    assert report.digest and wire["digest"] == report.digest


def test_the_wire_always_fits_48_kb():
    diag = make_diagnostic(tool="gnu", severity="error", file="src/x.cpp", line=1, message="m" * 390)
    big_trace = parse_dash_H("".join(". /work/sparklite/h%05d.h\n" % i for i in range(5000)),
                             root_file="/work/sparklite/src/x.cpp")
    report = _report(notes=tuple("note %d " % i + "n" * 250 for i in range(40)),
                     include_trace=relabel_trace(big_trace, source_root=SOURCE, build_dir=BUILD),
                     first_errors=(diag,) * 24)
    wire = build_report_to_wire(report)
    assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) <= 48_000
    assert wire["wire_truncated"]
    small = build_report_to_wire(report, max_bytes=8000)
    assert len(json.dumps(small, ensure_ascii=False, separators=(",", ":")).encode()) <= 8000
    assert small["status"] == "failed" and small["job_id"] == report.job_id


def test_statuses_and_isolation_labels_are_closed_sets():
    with pytest.raises(BuildDomainError):
        _report(status="ok")
    with pytest.raises(BuildDomainError):
        _report(isolation_truth="security_boundary_verified")
    assert _report(status="did_not_run", exit_code=None).status == "did_not_run"


def test_scrub_paths_and_trace_relabel():
    assert scrub_paths("In file included from /work/sparklite/src/a.cpp:1 and "
                       "/work/sparklite/build/ninja-debug/gen/x.h", source_root=SOURCE,
                       build_dir=BUILD) == "In file included from src/a.cpp:1 and <build>/gen/x.h"
    assert scrub_paths("C:\\SRC\\Spark\\a.cpp", source_root="C:/src/spark", build_dir="") == "a.cpp"
    trace = relabel_trace(parse_dash_H(". /work/sparklite/src/core/math.h\n.. /usr/include/cmath\n",
                                       root_file="/work/sparklite/src/core/math.cpp"),
                          source_root=SOURCE, build_dir=BUILD)
    assert trace.root_file == "src/core/math.cpp"
    assert trace.edges == (("src/core/math.cpp", "src/core/math.h", 1),
                           ("src/core/math.h", "<external>/cmath", 2))
