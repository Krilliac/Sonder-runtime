"""Typed preflight service and adapter coverage."""
from __future__ import annotations

import inspect
import pickle
import subprocess
import sys
from pathlib import Path

from sonder_runtime.adapters import preflight as preflight_adapter
from sonder_runtime.adapters.preflight_executor import PreflightExecutor
from sonder_runtime.application.ports.preflight import CheckResult, PreflightReport
from sonder_runtime.application.preflight import PreflightService
from sonder_runtime.platform.config import SonderConfig


ROOT = Path(__file__).resolve().parents[1]


def test_adapter_exposes_types_helpers_and_signature():
    assert preflight_adapter.SonderConfig is SonderConfig
    assert preflight_adapter.CheckResult is CheckResult
    assert preflight_adapter.PreflightReport is PreflightReport
    assert callable(preflight_adapter._check_disk_space)
    assert str(inspect.signature(preflight_adapter.run_preflight)) == (
        "(config: 'SonderConfig', *, check_ollama: 'bool' = True, "
        "ollama_timeout: 'float' = 5.0) -> 'PreflightReport'"
    )


def test_adapter_uses_packaged_configuration_boundary():
    source = (ROOT / "sonder_runtime" / "adapters" / "preflight.py").read_text(
        encoding="utf-8"
    )
    assert "from sonder_config import" not in source
    assert "from sonder_runtime.platform.config import SonderConfig" in source


def test_report_text_shape_properties_and_pickle_path_remain_stable():
    report = PreflightReport((
        CheckResult("required", True, True, "ready"),
        CheckResult("optional", False, False, "offline"),
    ))
    assert report.ok is True
    assert report.degraded is True
    assert report.as_dict() == {
        "ok": True,
        "degraded": True,
        "checks": [
            {"name": "required", "ok": True, "required": True, "detail": "ready"},
            {"name": "optional", "ok": False, "required": False, "detail": "offline"},
        ],
    }
    restored = pickle.loads(pickle.dumps(report))
    assert restored == report
    assert repr(report).startswith("PreflightReport(checks=(CheckResult(")
    assert CheckResult.__module__ == "sonder_runtime.adapters.preflight"
    assert PreflightReport.__module__ == "sonder_runtime.adapters.preflight"


def test_service_delegates_exact_options_and_result_identity():
    expected = PreflightReport((CheckResult("one", True, True, "ok"),))
    calls = []

    class Executor:
        def run(self, config, *, check_ollama=True, ollama_timeout=5.0):
            calls.append((config, check_ollama, ollama_timeout))
            return expected

    config = object()
    result = PreflightService(Executor()).run(
        config, check_ollama=False, ollama_timeout=0.25
    )
    assert result is expected
    assert calls == [(config, False, 0.25)]


def test_legacy_executor_import_is_lazy_in_fresh_process():
    code = "\n".join((
        "import sys",
        "import sonder_runtime.__main__",
        "from sonder_runtime.adapters.preflight_executor import PreflightExecutor",
        "assert PreflightExecutor is not None",
        "assert 'sonder_runtime.adapters.preflight' not in sys.modules",
        "assert 'sonder_migrations' not in sys.modules",
    ))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_executor_delegates_to_live_adapter_monkeypatch(monkeypatch):
    expected = PreflightReport((CheckResult("patched", True, True, "live"),))
    seen = []
    monkeypatch.setattr(
        preflight_adapter,
        "run_preflight",
        lambda config, **options: seen.append((config, options)) or expected,
    )
    config = object()
    result = PreflightExecutor().run(
        config, check_ollama=False, ollama_timeout=0.75
    )
    assert result is expected
    assert seen == [(
        config,
        {"check_ollama": False, "ollama_timeout": 0.75},
    )]


def test_entrypoint_has_no_root_preflight_dependency():
    source = (ROOT / "sonder_runtime" / "__main__.py").read_text(encoding="utf-8")
    assert "import sonder_preflight" not in source
    assert "from sonder_preflight" not in source
