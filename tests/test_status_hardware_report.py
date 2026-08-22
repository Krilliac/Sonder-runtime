import server


def test_status_includes_hardware_mode_without_exposing_prompt_data(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_inventory_rows_policy", lambda payload, endpoint: [])
    monkeypatch.setattr(server, "_get", lambda endpoint: {"models": []})
    monkeypatch.setattr(
        server.sonder_hardware,
        "get_profile",
        lambda **kwargs: {
            "hardware": {"gpu_vendor": "nvidia", "gpu_name": "RTX", "vram_free_gb": 14},
            "recommendation": {
                "capabilities": {
                    "gpu_vendor": "nvidia", "gpu_name": "RTX",
                    "vram_free_gb": 14, "backend_candidates": ("cpu", "ollama"),
                },
                "model_execution": {"mode": "gpu+ram-hybrid"},
            },
        },
    )
    result = server.status()

    assert "hardware: nvidia RTX; 14 GB free VRAM; backends=cpu,ollama; 30B=gpu+ram-hybrid" in result
