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


def test_backend_attestation_is_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("SONDER_NIGHTLY_BACKEND_ATTEST", raising=False)
    assert not nightly_self_improve._backend_attestation_enabled(
        types.SimpleNamespace(backend_attest=False)
    )
    assert nightly_self_improve._backend_attestation_enabled(
        types.SimpleNamespace(backend_attest=True)
    )


def test_backend_attestation_config_opt_in_is_explicit(monkeypatch):
    monkeypatch.setenv("SONDER_NIGHTLY_BACKEND_ATTEST", "yes")
    assert nightly_self_improve._backend_attestation_enabled(
        types.SimpleNamespace(backend_attest=False)
    )
    monkeypatch.setenv("SONDER_NIGHTLY_BACKEND_ATTEST", "0")
    assert not nightly_self_improve._backend_attestation_enabled(
        types.SimpleNamespace(backend_attest=False)
    )


@pytest.mark.parametrize("endpoint", [
    "https://example.invalid/api",
    "http://10.0.0.2:8080",
    "http://127.0.0.1:8080?secret=1",
])
def test_backend_attestation_rejects_non_loopback_or_ambiguous_endpoint(
    monkeypatch, tmp_path, endpoint,
):
    monkeypatch.setenv("SONDER_OPENAI_MODEL", "fixture")
    args = types.SimpleNamespace(
        backend_attest_base_url=endpoint,
        backend_attest_model="",
        backend_attest_timeout=5,
        backend_attest_evidence="",
    )
    paths = types.SimpleNamespace(state_path=lambda name: str(tmp_path / name))
    with pytest.raises(RuntimeError, match="loopback"):
        nightly_self_improve._run_backend_attestation(args, paths)


def test_backend_attestation_uses_typed_result_and_never_allows_cloud(
    monkeypatch, tmp_path,
):
    import scripts.backend_attest as backend_attest

    calls = []

    def fake_attest(gateway, **kwargs):
        calls.append((gateway, kwargs))
        return {"passed": ["chat", "cancellation"], "failed": [], "reasons": {}}

    monkeypatch.setattr(backend_attest, "attest", fake_attest)
    args = types.SimpleNamespace(
        backend_attest_base_url="http://127.0.0.1:11434",
        backend_attest_model="fixture",
        backend_attest_timeout=7,
        backend_attest_evidence="",
    )
    paths = types.SimpleNamespace(state_path=lambda name: str(tmp_path / name))
    result = nightly_self_improve._run_backend_attestation(args, paths)

    assert result.startswith("local model=fixture passed=chat,cancellation")
    assert calls[0][1]["cloud_allowed"] is False
    assert calls[0][1]["timeout_seconds"] == 7
    assert calls[0][1]["evidence_path"] == str(tmp_path / "backend-capabilities.json")


def test_backend_attestation_failure_is_a_nightly_failure_but_does_not_raise():
    failures = []
    messages = []
    result = nightly_self_improve._stage(
        messages.append,
        "backend-attestation",
        lambda: (_ for _ in ()).throw(RuntimeError("structured_json_invalid")),
        failures,
    )
    assert result is None
    assert failures == ["backend-attestation"]
    assert "structured_json_invalid" in messages[0]


def test_nightly_lock_fails_closed_when_path_is_inaccessible(tmp_path):
    messages = []
    path = tmp_path / "missing" / "nightly.lock"

    assert nightly_self_improve._claim_lock(path, messages.append) is None
    assert any("refusing nightly run" in message for message in messages)


def test_nightly_reports_failed_task_result_when_lock_cannot_be_opened(tmp_path, monkeypatch):
    import sonder_paths

    monkeypatch.setattr(nightly_self_improve.sys, "argv", ["nightly_self_improve.py"])
    monkeypatch.setattr(sonder_paths, "state_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(nightly_self_improve, "_claim_lock", lambda *_: None)

    assert nightly_self_improve.main() == 1


def test_nightly_does_not_reclaim_old_lock_owned_by_live_process(tmp_path, monkeypatch):
    path = tmp_path / "nightly.lock"
    path.write_text("4242", encoding="utf-8")
    old = nightly_self_improve.time.time() - 7 * 3600
    import os
    os.utime(path, (old, old))
    monkeypatch.setattr(nightly_self_improve, "_pid_state", lambda pid: "running")
    messages = []

    assert not nightly_self_improve._claim_lock(path, messages.append)
    assert path.read_text(encoding="utf-8") == "4242"
    assert any("still alive" in message for message in messages)


def test_nightly_reclaims_old_lock_when_owner_is_gone(tmp_path, monkeypatch):
    path = tmp_path / "nightly.lock"
    path.write_text("4242", encoding="utf-8")
    old = nightly_self_improve.time.time() - 7 * 3600
    import os
    os.utime(path, (old, old))

    monkeypatch.setattr(nightly_self_improve, "_pid_state", lambda pid: "gone")
    messages = []

    assert nightly_self_improve._claim_lock(path, messages.append)
    assert path.read_text(encoding="utf-8") == str(nightly_self_improve.os.getpid())


def test_windows_lock_owner_probe_never_terminates_a_live_process():
    if nightly_self_improve.os.name != "nt":
        pytest.skip("Windows process handle probe")
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        assert nightly_self_improve._pid_state(child.pid) == "running"
        assert child.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


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
        SESSION_NUM_CTX = 8192
        OLLAMA_POOL = types.SimpleNamespace(request=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("remote pool used")))
        calls = 0

        class ollama_endpoint:
            @staticmethod
            def is_loopback(base):
                return True

            @classmethod
            def open_url(cls, request, timeout, allow_remote):
                FakeServer.calls += 1
                if request.full_url.endswith("/api/chat"):
                    import json
                    payload = json.loads(request.data)
                    assert payload["messages"][0]["content"]
                    assert payload["options"]["num_ctx"] == 8192
                    return Response({"done": True})
                return Response({"models": []} if FakeServer.calls == 1 else {"models": [{"name": "local-code"}]})

        @staticmethod
        def _is_cloud_model_name(model):
            return False

    assert nightly_self_improve._prewarm_code_model(FakeServer()) == "ready model=local-code"


def test_nightly_prewarm_rejects_chat_error_even_when_model_is_resident(monkeypatch):
    calls = []

    class FakeServer:
        TIERS = {"code": "local-code"}
        BASE = "http://127.0.0.1:11434"

        @staticmethod
        def _is_cloud_model_name(model):
            return False

    def local_json(_server, path, _payload, _timeout):
        calls.append(path)
        if path == "/api/ps":
            return {"models": [{"name": "local-code"}]}
        return {"error": "CUDA error", "done": False}

    monkeypatch.setattr(nightly_self_improve, "_local_ollama_json", local_json)
    with pytest.raises(nightly_self_improve._CodeModelUnavailable, match="chat probe"):
        nightly_self_improve._prewarm_code_model(FakeServer())
    assert "/api/chat" in calls


def test_skip_campaign_still_prewarms_before_selfmod(monkeypatch):
    messages = []
    stages = []
    monkeypatch.setitem(sys.modules, "server", types.SimpleNamespace())
    monkeypatch.setattr(nightly_self_improve, "_preflight", lambda: ((), ()))
    monkeypatch.setattr(nightly_self_improve, "_backend_attestation_enabled", lambda _args: False)

    def stage(_log, name, _action, failures=None):
        stages.append(name)
        if name == "code-model-prewarm":
            failures.append(name)
            return None
        if name == "selfmod":
            pytest.fail("selfmod must not start after a failed chat probe")
        return "ok"

    monkeypatch.setattr(nightly_self_improve, "_stage", stage)
    args = types.SimpleNamespace(rounds=1, skip_campaign=True)
    assert nightly_self_improve._run_locked(args, messages.append, types.SimpleNamespace()) == 1
    assert "code-model-prewarm" in stages
    assert "selfmod" not in stages
    assert any("[selfmod] SKIPPED" in line for line in messages)


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


def test_nightly_campaign_surfaces_bounded_pitfall_error():
    class FakeServer:
        @staticmethod
        def campaign_generate_compile_execute_record(**_kwargs):
            return (
                "campaign generate/compile/execute/record: 21/24 passed\n"
                "by language: python=4/5\n"
                "first pitfall error: distillation store unavailable " + "x" * 500
            )

    logged = []
    args = types.SimpleNamespace(campaign_total=24)
    result = nightly_self_improve._run_campaign(FakeServer(), args, logged.append)

    assert result == "campaign generate/compile/execute/record: 21/24 passed"
    assert logged[0].startswith("[campaign] first pitfall error:")
    assert len(logged[0]) <= len("[campaign] ") + 300


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
