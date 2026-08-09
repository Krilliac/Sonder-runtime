from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_cross_platform_launcher_preserves_explicit_python_override():
    shell = _read("sonder-runtime.sh")
    cmd = _read("sonder-runtime.cmd")

    assert "sonder_configured_python=${SONDER_PYTHON:-}" in shell
    assert 'if [ -z "$SONDER_PYTHON" ] && [ -n "$SONDER_ENGINE_ROOT" ]' in shell
    assert '"$SONDER_RUNTIME_ROOT/venv/bin/python"' in shell
    assert "set \"SONDER_CONFIGURED_PYTHON=%SONDER_PYTHON%\"" in cmd
    assert "if not defined SONDER_PYTHON if defined SONDER_ENGINE_ROOT" in cmd


def test_macos_launcher_preserves_legacy_store_until_native_store_exists():
    shell = _read("sonder-runtime.sh")

    assert 'sonder_native_home="${HOME:-$SONDER_RUNTIME_ROOT}/Library/Application Support/sonder"' in shell
    assert 'sonder_legacy_home="${HOME:-$SONDER_RUNTIME_ROOT}/.local/share/sonder"' in shell
    assert '[ -d "$sonder_legacy_home" ] && [ ! -e "$sonder_native_home" ]' in shell


def test_windows_helpers_accept_explicit_python_before_repo_venv():
    remote = _read("sonder-remote.cmd")
    tests = _read("scripts/run-tests.cmd")
    selfmod = _read("scripts/start-selfmod.ps1")

    assert "set \"PYTHON=%SONDER_PYTHON%\"" in remote
    assert "set \"PY=%SONDER_PYTHON%\"" in tests
    assert "$env:SONDER_PYTHON" in selfmod
    assert "Get-Command python" in selfmod


def test_shipped_product_sources_contain_no_machine_specific_drive_root():
    product_files = (
        "code_runner.py",
        "verifiers.py",
        "server.py",
        "master_orchestrator.py",
        "examples/codegen-arena-shooter/build_with_sonder.py",
        "scripts/run-tests.cmd",
    )
    for relative in product_files:
        text = _read(relative)
        assert "D:\\" not in text, relative
        assert "D:/" not in text, relative
        assert "C:\\Users\\natew" not in text, relative
