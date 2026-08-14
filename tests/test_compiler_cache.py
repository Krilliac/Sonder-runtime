import io
import json

import compiler_cache
import server


def test_status_is_fixed_bounded_and_scrubs_paths(monkeypatch):
    captured = {}

    monkeypatch.setattr(compiler_cache.shutil, "which", lambda name: "C:\\tools\\sccache.exe")

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            self.stdout = io.StringIO(
                "Compile requests                      42\n"
                "Cache hits                            40\n"
                "Cache location                  Local disk: C:\\private\\cache\n"
                "Version (client)                0.16.0\n"
            )
            self.stderr = io.StringIO("secret-looking error text")
            self._returncode = 0

        def poll(self): return self._returncode
        def wait(self, timeout=None): return self._returncode
        def kill(self): self._returncode = -9
        @property
        def returncode(self): return self._returncode

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return FakeProcess(argv, **kwargs)

    monkeypatch.setattr(compiler_cache.subprocess, "Popen", fake_popen)
    result = compiler_cache.status()

    assert captured["argv"] == ["C:\\tools\\sccache.exe", "--show-stats"]
    assert captured["shell"] is False
    assert "timeout" not in captured
    assert result == {
        "ok": True,
        "available": True,
        "status": "ok",
        "stats": [
            "Compile requests 42",
            "Cache hits 40",
            "Version (client) 0.16.0",
        ],
    }
    assert "private" not in str(result)
    assert "secret" not in str(result)


def test_status_handles_missing_and_timeout(monkeypatch):
    monkeypatch.setattr(compiler_cache.shutil, "which", lambda name: None)
    assert compiler_cache.status()["status"] == "not_installed"

    monkeypatch.setattr(compiler_cache.shutil, "which", lambda name: "sccache")
    monkeypatch.setattr(compiler_cache, "_run_bounded", lambda argv: ("timeout", ""))
    assert compiler_cache.status()["status"] == "timeout"


def test_bounded_capture_stops_at_shared_output_limit(monkeypatch):
    class LoudProcess:
        def __init__(self, *args, **kwargs):
            self.stdout = io.StringIO("x" * (compiler_cache._MAX_OUTPUT_CHARS + 1))
            self.stderr = io.StringIO("")
            self._returncode = None
            self.killed = False

        def poll(self): return self._returncode
        def wait(self, timeout=None): return self._returncode
        def kill(self):
            self.killed = True
            self._returncode = -9
        @property
        def returncode(self): return self._returncode

    proc = LoudProcess()
    monkeypatch.setattr(compiler_cache.subprocess, "Popen", lambda *args, **kwargs: proc)
    outcome, output = compiler_cache._run_bounded(["sccache", "--show-stats"])
    assert outcome == "output_limit"
    assert len(output) == compiler_cache._MAX_OUTPUT_CHARS
    assert proc.killed


def test_direct_tool_returns_only_sanitized_cache_metrics(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        server.compiler_cache,
        "status",
        lambda: {"ok": True, "available": True, "status": "ok", "stats": ["Cache hits 2"]},
    )
    monkeypatch.setattr(
        server,
        "_record_direct_tool",
        lambda *args, **kwargs: recorded.update(name=args[0], output=kwargs["output"]),
    )
    assert json.loads(server.compiler_cache_status()) == {
        "available": True,
        "ok": True,
        "stats": ["Cache hits 2"],
        "status": "ok",
    }
    assert recorded == {
        "name": "compiler_cache_status",
        "output": '{"available":true,"ok":true,"stats":["Cache hits 2"],"status":"ok"}',
    }
