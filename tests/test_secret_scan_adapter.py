from __future__ import annotations

from pathlib import Path

import sonder_runtime.adapters.secret_scan as secret_scan


def test_secret_scan_redacts_findings(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_scan.file_ops, "resolve_path", lambda *args, **kwargs: Path(tmp_path))
    (tmp_path / "config.env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")
    result = secret_scan.scan(".")
    assert result["ok"] is True
    assert result["findings"][0]["match"] == "[REDACTED CREDENTIAL]"
    assert "super-secret" not in secret_scan.format_result(result)


def test_secret_scan_reports_bounded_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_scan.file_ops, "resolve_path", lambda *args, **kwargs: Path(tmp_path))
    (tmp_path / "notes.txt").write_text("nothing", encoding="utf-8")
    clock = iter((100.0, 102.0))
    monkeypatch.setattr(secret_scan.time, "monotonic", lambda: next(clock))
    result = secret_scan.scan(".", timeout=1)
    assert result["truncated"] is True
