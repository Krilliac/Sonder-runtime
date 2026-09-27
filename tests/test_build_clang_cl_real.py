"""clang-cl-18 on Linux: ``/Zs /showIncludes`` through the real launcher and
collector gives an IncludeTrace and MSVC-shaped diagnostics without C####
codes (F3: the codeless form parses through the build domain's fallback)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.test_build_launcher_real import LauncherStack

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("clang-cl-18") is None and shutil.which("clang-cl") is None,
                       reason="clang-cl is not installed"),
]


def test_clang_cl_show_includes_and_codeless_diagnostics(tmp_path):
    from dataclasses import replace

    from sonder_runtime.adapters.build.collector import BuildOutputCollector

    compiler = shutil.which("clang-cl-18") or shutil.which("clang-cl")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "local.h").write_text("#pragma once\nint y;\n")
    (project / "cc.cpp").write_text('#include "local.h"\nint f() { return x; }\n')
    stack = LauncherStack(tmp_path)
    plan = stack.plan([compiler, "/Zs", "/showIncludes", "/nologo", "cc.cpp"], cwd=project,
                      action="include_trace")
    plan = replace(plan, project_root=str(project), build_dir=str(project / "build"),
                   file_label="cc.cpp", trace_family="msvc")
    job = stack.start(plan)
    record, exit_code = stack.finish(job)
    collector = BuildOutputCollector(str(stack.run_root))
    report = collector.collect(job, stack.launcher.metadata(job), None, record=record, exit_code=exit_code)
    assert report.status == "failed"
    assert report.include_trace is not None
    assert any(header.endswith("local.h") for header in report.include_trace.headers())
    errors = [item for item in report.first_errors if item.severity == "error"]
    assert errors, "clang-cl's codeless 'file(l,c): error:' form must parse (F3)"
    assert (errors[0].file, errors[0].line, errors[0].col) == ("cc.cpp", 2, 18)
    assert errors[0].code == "" and "undeclared identifier" in errors[0].message
    assert Path(plan.log_file).read_text().startswith("Note: including file:")
