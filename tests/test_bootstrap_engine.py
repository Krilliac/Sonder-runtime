import bootstrap_engine


def test_choose_model_by_ram():
    assert bootstrap_engine.choose_model(2) == "qwen2.5-coder:1.5b"
    assert bootstrap_engine.choose_model(4) == "qwen2.5-coder:3b"
    assert bootstrap_engine.choose_model(8) == "qwen2.5-coder:7b"


def test_choose_model_env_override(monkeypatch):
    monkeypatch.setenv("TRILOBITE_BASE_MODEL", "custom:model")
    assert bootstrap_engine.choose_model(1) == "custom:model"
