from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_primary_launchers_delegate_engine_checks_to_gated_python():
    for name in (
        "sonder.cmd", "sonder-serve.cmd", "sonder-serve.sh",
        "endless-train.cmd",
    ):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "sonder_headless.py" in text
        assert " engine" in text
        lowered = text.lower()
        assert '%sonder_ollama_exe%" list' not in lowered
        assert 'sonder_ollama_exe:-ollama}" show' not in lowered


def test_server_deploy_pins_ollama_clients_to_preflight_origin():
    text = (ROOT / "deploy_sonder.sh").read_text(encoding="utf-8")

    assert "configured_origin(allow_remote=False)" in text
    assert text.count('OLLAMA_HOST="$CLIENT_OLLAMA_HOST" ollama ') == 3
