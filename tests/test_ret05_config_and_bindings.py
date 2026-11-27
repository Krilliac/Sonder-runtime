"""RET-05 Subsystem 1 tests: typed [inference] config, SI-only default bindings, unsafe lab, and isolated HTTP runtimes."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceGateway
from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
from sonder_runtime.adapters.observability.work_narration_facts import configured_host
from sonder_runtime.adapters.provider_bindings import (
    ALLOWED_FALLBACKS,
    PROVIDER_TIERS,
    normalize_provider,
    provider_bindings_from_env,
)
from sonder_runtime.bootstrap import managed_http_runtime, owned_http_runtime
from sonder_runtime.domain import ollama_policy
from sonder_runtime.interfaces.orchestration_commands import inherited_remote_consent
from sonder_runtime.platform import config as sonder_config
from sonder_runtime.platform import unsafe_lab_policy


def test_inference_config_defaults_and_redaction(tmp_path):
    key_file = tmp_path / "si.token"
    key_file.write_text("fake-si-token-value-1234567890", encoding="utf-8")
    toml_path = tmp_path / "sonder.toml"
    toml_path.write_text(
        "[inference]\n"
        'url = "https://10.77.0.2:11437"\n'
        "allow_remote = true\n"
        f'api_key_file = "{key_file.as_posix()}"\n'
        'model = "qwen3.6:35b"\n'
        'embed_model = "nomic-embed-text"\n'
        'private_workers = "[{\\"url\\": \\"https://10.77.0.2:8443\\"}]"\n'
        "max_inflight = 4\n",
        encoding="utf-8",
    )

    loaded = sonder_config.load_config(toml_path, env={})
    assert loaded.inference.url == "https://10.77.0.2:11437"
    assert loaded.inference.allow_remote is True
    assert loaded.inference.model == "qwen3.6:35b"
    assert loaded.inference.embed_model == "nomic-embed-text"
    assert loaded.inference.max_inflight == 4

    redacted = loaded.as_redacted_dict()
    assert "inference" in redacted
    assert redacted["inference"]["api_key_file"] == "[set]"
    assert redacted["inference"]["private_workers"] == "[set]"
    assert "fake-si-token" not in str(redacted)
    assert str(key_file.resolve()) in loaded.private_source_paths


def test_inference_config_rejects_remote_without_consent_or_tls(tmp_path):
    toml_path = tmp_path / "remote.toml"
    toml_path.write_text(
        '[inference]\nurl = "https://10.77.0.2:11437"\nallow_remote = false\n',
        encoding="utf-8",
    )
    with pytest.raises(sonder_config.ConfigError, match=r"\[inference\]\.url"):
        sonder_config.load_config(toml_path, env={})

    http_remote = tmp_path / "http_remote.toml"
    http_remote.write_text(
        '[inference]\nurl = "http://10.77.0.2:11437"\nallow_remote = true\n',
        encoding="utf-8",
    )
    with pytest.raises(sonder_config.ConfigError, match="https"):
        sonder_config.load_config(http_remote, env={})


def test_default_config_and_policy_use_si_port_11437():
    cfg = sonder_config.SonderConfig()
    assert cfg.inference.url == "http://127.0.0.1:11437"
    assert "11434" not in cfg.ollama.url
    assert ollama_policy.DEFAULT_HOST == "127.0.0.1:11437"


def test_provider_bindings_default_to_sonder_inference_and_retire_local_ollama(monkeypatch):
    monkeypatch.delenv("SONDER_MODEL_BACKEND", raising=False)
    monkeypatch.delenv("SONDER_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("SONDER_INFERENCE_FALLBACK", raising=False)

    bindings = provider_bindings_from_env({})
    assert bindings.default_generation_provider == "sonder_inference"
    assert bindings.embedding_provider == "sonder_inference"
    assert dict(bindings.tier_providers) == {tier: "sonder_inference" for tier in PROVIDER_TIERS}
    assert dict(ALLOWED_FALLBACKS) == {}

    with pytest.raises(ValueError, match="sonder-inference"):
        normalize_provider("ollama")

    with pytest.raises(ValueError, match="SONDER_INFERENCE_FALLBACK"):
        provider_bindings_from_env({
            "SONDER_MODEL_BACKEND": "sonder-inference",
            "SONDER_INFERENCE_FALLBACK": "ollama",
        })

    assert isinstance(build_model_gateway(), SonderInferenceGateway)


def test_unsafe_lab_enforces_loopback_si_and_forbids_private_workers():
    ack = unsafe_lab_policy.ACKNOWLEDGEMENT
    ok_env = {
        unsafe_lab_policy.ACK_ENV: ack,
        "SONDER_HOST": "127.0.0.1",
        "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:11437",
    }
    assert unsafe_lab_policy.validation_error(ok_env) == ""

    remote_si = dict(ok_env, SONDER_INFERENCE_BASE_URL="https://10.77.0.2:11437")
    assert "SONDER_INFERENCE_BASE_URL" in unsafe_lab_policy.validation_error(remote_si)

    with_workers = dict(ok_env, SONDER_INFERENCE_PRIVATE_WORKERS='[{"url":"https://10.77.0.2:8443"}]')
    assert "SONDER_INFERENCE_PRIVATE_WORKERS" in unsafe_lab_policy.validation_error(with_workers)


def test_managed_and_owned_http_runtimes_isolate_sonder_inference():
    managed_src = inspect.getsource(managed_http_runtime)
    owned_src = inspect.getsource(owned_http_runtime)
    for src in (managed_src, owned_src):
        assert "SONDER_INFERENCE_BASE_URL" in src
        assert "SONDER_INFERENCE_PRIVATE_WORKERS" in src
        assert "SONDER_ALLOW_REMOTE_INFERENCE" in src
        assert "OLLAMA_HOST" not in src
        assert "SONDER_OLLAMA_WORKERS" not in src


def test_orchestration_remote_consent_reads_inference_config():
    app = SimpleNamespace(
        config=SimpleNamespace(
            inference=SimpleNamespace(allow_remote=True),
            ollama=SimpleNamespace(allow_remote=False),
        )
    )
    assert inherited_remote_consent(False, True, lambda: app) is True
    assert inherited_remote_consent(False, False, lambda: app) is False


def test_work_narration_reports_sonder_inference_not_ollama(monkeypatch):
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:11437")
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    text = configured_host({}, tier="code")
    assert "sonder_inference" in text
    assert "127.0.0.1:11437" in text
    assert "11434" not in text
