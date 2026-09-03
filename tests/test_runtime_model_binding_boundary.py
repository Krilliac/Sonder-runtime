"""Runtime model binding checks live in the domain; root names are aliases."""
import server
from sonder_runtime.domain import runtime_model_binding as binding


def test_root_names_are_identity_preserving_aliases():
    assert server._runtime_model_is_installed is binding.model_is_installed
    assert server._runtime_model_capability_error is binding.model_capability_error


def test_installed_matching_respects_ollama_latest_tag_semantics():
    installed = ["Gemma3:12b", "phi4:latest", "qwen:7b"]
    assert binding.model_is_installed("gemma3:12b", installed)
    assert binding.model_is_installed("phi4", installed)
    assert binding.model_is_installed("PHI4:latest", installed)
    assert not binding.model_is_installed("qwen", installed)
    assert not binding.model_is_installed("qwen:latest", installed)
    assert not binding.model_is_installed("gemma3", installed)
    assert not binding.model_is_installed("", installed)


def test_capability_errors_use_the_catalog_record_of_the_installed_model():
    records = [
        ("embed:latest", {"capabilities": ["embedding"]}),
        ("vision:latest", {"capabilities": ["vision"]}),
        ("chat:latest", {"capabilities": ["chat"]}),
        ("unknown:latest", {}),
    ]
    assert binding.model_capability_error("code", "missing", records) == ""
    assert binding.model_capability_error("code", "chat", records) == ""
    assert binding.model_capability_error("code", "unknown", records) == ""
    assert binding.model_capability_error("code", "embed", records) == "embedding-only capability"
    assert binding.model_capability_error("code", "vision", records) == "vision-only capability"
    assert binding.model_capability_error("vision", "vision", records) == ""
    assert binding.model_capability_error("vision", "embed", records) == "embedding-only capability"
