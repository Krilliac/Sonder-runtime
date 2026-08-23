from sonder_runtime.adapters.accelerators.backend_probe import (
    probe_backend_inventory,
    select_backend,
)


def test_inventory_reports_presence_without_leaking_executable_paths():
    executables = {"ollama", "llama-server"}

    def which(name):
        return "C:/private/bin/%s.exe" % name if name in executables else None

    result = probe_backend_inventory(
        which=which,
        find_spec=lambda name: object() if name == "vllm" else None,
    )

    assert result["ollama"] == {
        "installed": True,
        "evidence": ("executable:ollama",),
        "readiness": "not-probed",
    }
    assert result["llamacpp"]["installed"] is True
    assert result["vllm"]["evidence"] == ("module:vllm",)
    assert all("C:/" not in str(row) for row in result.values())


def test_inventory_probe_failures_degrade_to_not_installed():
    def fail(_name):
        raise OSError("probe unavailable")

    result = probe_backend_inventory(which=fail, find_spec=fail)

    assert all(row["installed"] is False for row in result.values())
    assert all(row["readiness"] == "not-probed" for row in result.values())


def test_selection_prefers_format_specific_cuda_backend_but_stays_advisory():
    inventory = probe_backend_inventory(
        which=lambda name: "C:/bin/%s" % name if name == "llama-server" else None,
        find_spec=lambda _name: None,
    )

    result = select_backend(inventory, cuda_available=True, model_format="gguf")

    assert result["backend"] == "llamacpp"
    assert result["accelerated"] is True
    assert "readiness" not in result


def test_selection_falls_back_to_cpu_when_no_provider_is_present():
    result = select_backend({}, cuda_available=True, model_format="gguf")

    assert result["backend"] == "cpu"
    assert result["accelerated"] is False
    assert result["candidates"] == ("cpu",)
