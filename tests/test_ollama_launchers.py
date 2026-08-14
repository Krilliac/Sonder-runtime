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


def test_terminal_launcher_distinguishes_an_existing_unmanaged_server_from_failure():
    text = (ROOT / "sonder.cmd").read_text(encoding="utf-8")

    assert 'findstr /C:"sonder: already listening"' in text
    assert "using an existing local API server" in text


def test_terminal_launcher_accepts_managed_server_verified_by_late_status_probe():
    text = (ROOT / "sonder.cmd").read_text(encoding="utf-8")

    assert 'findstr /C:"sonder api: listening on"' in text
    assert "became ready after the startup probe" in text


def test_windows_launcher_uses_a_per_invocation_bootstrap_log():
    text = (ROOT / "sonder.cmd").read_text(encoding="utf-8")

    assert 'set "SONDER_BOOT_LOG=%TEMP%\\sonder-bootstrap-%RANDOM%-%RANDOM%.log"' in text
    assert 'set "SONDER_BOOT_LOG=%TEMP%\\sonder-bootstrap.log"' not in text


def test_server_deploy_pins_ollama_clients_to_preflight_origin():
    text = (ROOT / "deploy_sonder.sh").read_text(encoding="utf-8")

    assert "configured_origin(allow_remote=False)" in text
    assert text.count('OLLAMA_HOST="$CLIENT_OLLAMA_HOST" ollama ') == 3


def test_terminal_launcher_cleans_only_its_generated_bootstrap_log():
    text = (ROOT / "sonder.cmd").read_text(encoding="utf-8")

    assert "SONDER_BOOT_LOG_GENERATED=1" in text
    assert "if defined SONDER_BOOT_LOG_GENERATED if exist" in text
    assert 'del /q "%SONDER_BOOT_LOG%"' in text
