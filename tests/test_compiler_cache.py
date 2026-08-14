import subprocess
import json

import compiler_cache
import server


def test_status_is_fixed_bounded_and_scrubs_paths(monkeypatch):
    captured = {}

    monkeypatch.setattr(compiler_cache.shutil, "which", lambda name: "C:\\tools\\sccache.exe")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv, 0,
            stdout=(
                "Compile requests                      42\n"
                "Cache hits                            40\n"
                "Cache location                  Local disk: C:\\private\\cache\n"
                "Version (client)                0.16.0\n"
            ),
            stderr="secret-looking error text",
        )

    monkeypatch.setattr(compiler_cache.subprocess, "run", fake_run)
    result = compiler_cache.status()

    assert captured["argv"] == ["C:\\tools\\sccache.exe", "--show-stats"]
    assert captured["shell"] is False
    assert captured["timeout"] == 3
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
    monkeypatch.setattr(
        compiler_cache.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], 3)),
    )
    assert compiler_cache.status()["status"] == "timeout"


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
