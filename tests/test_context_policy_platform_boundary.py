import context_policy
from sonder_runtime.platform import context_policy as canonical


def test_root_context_policy_is_canonical_platform_module():
    assert context_policy is canonical
    assert context_policy.parse_size("32k") == 32000


def test_platform_policy_reads_live_kv_cache_environment(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q4_0")

    assert canonical.default_context() == 32768
    assert canonical.native() == 32768


def test_platform_policy_preserves_virtual_clamp(monkeypatch):
    monkeypatch.setenv("SONDER_NATIVE_CONTEXT_MAX", "256k")
    monkeypatch.setenv("SONDER_VIRTUAL_CONTEXT_MAX", "1m")

    result = canonical.policy("1m")

    assert result == {
        "requested": 1_000_000,
        "native": 256_000,
        "native_max": 256_000,
        "virtual_max": 1_000_000,
        "virtual": True,
        "mode": "virtual",
    }
