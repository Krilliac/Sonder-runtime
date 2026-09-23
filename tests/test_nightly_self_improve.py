from pathlib import Path
import sys
import types

import pytest

from scripts import nightly_self_improve


def test_nightly_rehomes_absolute_paths_from_another_checkout(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    other = (tmp_path / "other").resolve()
    other.mkdir()
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(other / "emotion_vectors.json"))
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(other / "system_profile.md"))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ("SONDER_EMOTION_VECTORS", "SONDER_SYSTEM_PROFILE")
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(root / "system_profile.md")


def test_nightly_preserves_an_in_checkout_override(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    custom = root / "custom-profile.md"
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", "emotion_vectors.json")
    monkeypatch.setenv("SONDER_SYSTEM_PROFILE", str(custom))

    rebound = nightly_self_improve._bind_workspace_config_paths(root)

    assert rebound == ()
    assert nightly_self_improve.os.environ["SONDER_EMOTION_VECTORS"] == str(root / "emotion_vectors.json")
    assert nightly_self_improve.os.environ["SONDER_SYSTEM_PROFILE"] == str(custom)


def test_nightly_rejects_an_escaping_checkout_default(tmp_path, monkeypatch):
    root = (tmp_path / "runtime").resolve()
    root.mkdir()
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    default = root / "emotion_vectors.json"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == default:
            return outside / default.name
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setenv("SONDER_EMOTION_VECTORS", str(outside / default.name))

    with pytest.raises(ValueError, match="workspace default escapes checkout"):
        nightly_self_improve._bind_workspace_config_paths(root)


def test_nightly_exports_toml_workers_when_env_blank(tmp_path, monkeypatch):
    cfg = tmp_path / "sonder.toml"
    cfg.write_text(
        "\n".join([
            "schema_version = 1",
            'profile = "workstation-local"',
            "[ollama]",
            'url = "http://127.0.0.1:11434"',
            "allow_remote = true",
            'workers = ["https://10.77.0.2:8443"]',
        ]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SONDER_OLLAMA_WORKERS", raising=False)
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    monkeypatch.delenv("SONDER_TRUSTED_ORIGINS", raising=False)

    bound = nightly_self_improve._bind_ollama_pool_from_config(cfg)

    assert "SONDER_OLLAMA_WORKERS" in bound
    assert nightly_self_improve.os.environ["SONDER_OLLAMA_WORKERS"] == "https://10.77.0.2:8443"
    assert nightly_self_improve.os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] == "1"


def test_nightly_keeps_explicit_worker_env(tmp_path, monkeypatch):
    cfg = tmp_path / "sonder.toml"
    cfg.write_text(
        "schema_version = 1\n[ollama]\nallow_remote = true\n"
        'workers = ["https://10.77.0.2:8443"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_OLLAMA_WORKERS", "http://127.0.0.1:11435")

    assert nightly_self_improve._bind_ollama_pool_from_config(cfg) == ()
    assert nightly_self_improve.os.environ["SONDER_OLLAMA_WORKERS"] == "http://127.0.0.1:11435"


def test_nightly_binds_ca_from_toml_even_with_explicit_workers(tmp_path, monkeypatch):
    cfg = tmp_path / "sonder.toml"
    ca = tmp_path / "ca.pem"
    ca.write_text("certificate", encoding="ascii")
    cfg.write_text(
        "schema_version = 1\n[ollama]\n"
        'workers = ["https://10.77.0.2:8443"]\n'
        'ca_bundle = "' + str(ca).replace("\\", "\\\\") + '"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_OLLAMA_WORKERS", "http://127.0.0.1:11435")
    monkeypatch.delenv("SONDER_OLLAMA_CA_BUNDLE", raising=False)

    bound = nightly_self_improve._bind_ollama_pool_from_config(cfg)

    assert "SONDER_OLLAMA_WORKERS" not in bound
    assert "SONDER_OLLAMA_CA_BUNDLE" in bound
    assert nightly_self_improve.os.environ["SONDER_OLLAMA_CA_BUNDLE"] == str(ca)


def test_nightly_preflight_is_provider_and_workspace_binding_only(monkeypatch):
    calls = []

    def bind_workspace(root):
        calls.append(("workspace", root))
        return ("SONDER_EMOTION_VECTORS",)

    def bind_workers():
        calls.append(("ollama",))
        return ("SONDER_OLLAMA_WORKERS",)

    monkeypatch.setattr(nightly_self_improve, "_bind_workspace_config_paths", bind_workspace)
    monkeypatch.setattr(nightly_self_improve, "_bind_ollama_pool_from_config", bind_workers)

    rebound, workers = nightly_self_improve._preflight(Path("C:/nightly"))

    assert rebound == ("SONDER_EMOTION_VECTORS",)
    assert workers == ("SONDER_OLLAMA_WORKERS",)
    assert calls == [("workspace", Path("C:/nightly")), ("ollama",)]


def test_nightly_stage_records_critical_failure():
    failures = []
    messages = []

    result = nightly_self_improve._stage(
        messages.append, "campaign", lambda: (_ for _ in ()).throw(RuntimeError("down")), failures
    )

    assert result is None
    assert failures == ["campaign"]
    assert "FAILED" in messages[0]


def test_nightly_classifies_known_blocking_results_but_keeps_intentional_skips():
    cases = [
        ("selfmod", "working tree dirty (4 path(s)); none was started", True),
        ("selfmod", "selfmod disabled", False),
        ("selfmod", "no actionable objective proposed", False),
    ]
    for name, result, expected in cases:
        assert bool(nightly_self_improve._blocking_result(name, result)) is expected


def test_nightly_prewarm_waits_for_configured_code_model(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            import json
            return json.dumps(self.payload).encode()

    class FakeServer:
        TIERS = {"code": "local-code"}
        BASE = "http://127.0.0.1:11434"
        OLLAMA_POOL = types.SimpleNamespace(request=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("remote pool used")))
        calls = 0

        class ollama_endpoint:
            @staticmethod
            def is_loopback(base):
                return True

            @classmethod
            def open_url(cls, request, timeout, allow_remote):
                FakeServer.calls += 1
                if request.full_url.endswith("/api/generate"):
                    return Response({"done": True})
                return Response({"models": []} if FakeServer.calls == 1 else {"models": [{"name": "local-code"}]})

        @staticmethod
        def _is_cloud_model_name(model):
            return False

    assert nightly_self_improve._prewarm_code_model(FakeServer()) == "ready model=local-code"


def test_nightly_prewarm_reports_readiness_failure():
    class FakeServer:
        TIERS = {"code": "local-code"}
        BASE = "http://127.0.0.1:11434"

        class ollama_endpoint:
            @staticmethod
            def is_loopback(base):
                return True

            @staticmethod
            def open_url(*_args, **_kwargs):
                raise TimeoutError("runner unavailable")

        @staticmethod
        def _is_cloud_model_name(model):
            return False

    with pytest.raises(nightly_self_improve._CodeModelUnavailable, match="readiness probe failed"):
        nightly_self_improve._prewarm_code_model(FakeServer())


def test_nightly_campaign_uses_one_worker():
    captured = {}

    class FakeServer:
        @staticmethod
        def campaign_generate_compile_execute_record(**kwargs):
            captured.update(kwargs)
            return "campaign ok"

    args = types.SimpleNamespace(campaign_total=4)
    assert nightly_self_improve._run_campaign(FakeServer(), args) == "campaign ok"
    assert captured["max_workers"] == 1


def test_nightly_skips_all_model_stages_after_prewarm_failure():
    class FakeServer:
        TIERS = {"code": "local-code"}
        BASE = "http://127.0.0.1:11434"

        class ollama_endpoint:
            @staticmethod
            def is_loopback(base):
                return True

            @staticmethod
            def open_url(*_args, **_kwargs):
                raise TimeoutError("runner unavailable")

        @staticmethod
        def _is_cloud_model_name(model):
            return False

        def campaign_generate_compile_execute_record(self, **_kwargs):
            raise AssertionError("campaign must be skipped")

        def campaign_repo_repair(self, **_kwargs):
            raise AssertionError("repo repair must be skipped")

    failures = []
    messages = []
    args = types.SimpleNamespace(campaign_total=4, repair_total=2)

    assert not nightly_self_improve._run_code_model_stages(
        FakeServer(), args, messages.append, failures,
    )
    assert failures == ["code-model-prewarm"]
    assert any("campaign" in message and "SKIPPED" in message for message in messages)
    assert any("repo-repair" in message and "SKIPPED" in message for message in messages)


@pytest.mark.parametrize("failure", [ImportError("server import failed"), RuntimeError("stage failed")])
def test_nightly_cleans_lock_when_locked_run_raises(tmp_path, monkeypatch, failure):
    state = tmp_path / "state"
    state.mkdir()
    fake_paths = types.SimpleNamespace(
        state_path=lambda name: str(state / name),
    )
    monkeypatch.setitem(sys.modules, "sonder_paths", fake_paths)
    monkeypatch.setattr(nightly_self_improve, "_run_locked", lambda *args: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(sys, "argv", ["nightly_self_improve.py"])

    assert nightly_self_improve.main() == 1
    assert not (state / "nightly.lock").exists()
    log = next((state / "nightly-logs").glob("*.log")).read_text(encoding="utf-8")
    assert "nightly run failed" in log
