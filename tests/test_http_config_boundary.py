"""HTTP configuration boundary coverage."""
from __future__ import annotations

from pathlib import Path

import sonder_config
import sonder_runtime.interfaces.http.serve as serve
import sonder_runtime.platform.config as runtime_config


ROOT = Path(__file__).resolve().parents[1]


def test_http_uses_packaged_configuration_boundary():
    source = (ROOT / "sonder_runtime" / "interfaces" / "http" / "serve.py").read_text(
        encoding="utf-8"
    )
    assert "import sonder_config" not in source
    assert "import sonder_runtime.platform.config as runtime_config" in source
    assert "runtime_config.MIN_API_KEY_LENGTH" in source


def test_packaged_boundary_preserves_configuration_identity_and_policy_defaults():
    assert runtime_config.SonderConfig is sonder_config.SonderConfig
    assert runtime_config.load_config is sonder_config.load_config
    assert runtime_config.MIN_API_KEY_LENGTH == sonder_config.MIN_API_KEY_LENGTH == 24


def test_http_reads_the_packaged_policy_at_call_time(monkeypatch):
    monkeypatch.setattr(runtime_config, "MIN_API_KEY_LENGTH", 40)
    assert serve.runtime_config.MIN_API_KEY_LENGTH == 40
