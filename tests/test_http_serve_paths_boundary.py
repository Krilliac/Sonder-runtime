"""Regression coverage for the packaged HTTP path boundary."""

from pathlib import Path

import sonder_runtime.interfaces.http.serve as serve


def test_http_interface_uses_packaged_paths_for_default_home():
    source = Path(serve.__file__).read_text(encoding="utf-8")

    assert "import sonder_paths" not in source
    assert "server.sonder_paths" not in source
    assert "sonder_runtime.platform.paths" in source


def test_local_server_log_tail_uses_packaged_default_home(monkeypatch, tmp_path):
    log_path = tmp_path / "run" / "sonder_serve.log"
    log_path.parent.mkdir()
    log_path.write_text("server started\n", encoding="utf-8")
    monkeypatch.setattr(serve.runtime_paths, "default_home", lambda: tmp_path)

    assert serve._local_server_log_tail() == "server started\n"
