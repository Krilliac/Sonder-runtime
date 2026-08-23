from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_release_smoke_runbook_documents_both_platform_scripts():
    runbook = _text("docs/runbooks/release-smoke-check.md")

    assert "scripts/release_smoke.sh" in runbook
    assert "scripts\\release_smoke.ps1" in runbook
    assert "check_release_version.py" in runbook
    assert "sonder_runtime smoke --skip-ollama" in runbook


def test_release_version_policy_links_to_the_smoke_check():
    policy = _text("docs/runbooks/release-version-policy.md")

    assert "release-smoke-check.md" in policy


def test_workstation_local_runbook_documents_the_windows_installer_and_smoke_check():
    runbook = _text("docs/runbooks/install-workstation-local.md")

    assert "packaging\\install_workstation_local.ps1" in runbook
    assert "-SkipModelAlias" in runbook
    assert "-Force" in runbook
    assert "release-smoke-check.md" in runbook
    # Every numbered step's bash block has a documented PowerShell counterpart
    # (the one-shot installer intro above step 1 is PowerShell-only by design).
    assert runbook.count("```bash") == 3
    assert runbook.count("```powershell") == 4


def test_installer_scripts_referenced_in_docs_actually_exist():
    for relative in (
        "packaging/install_workstation_local.ps1",
        "scripts/release_smoke.ps1",
        "scripts/release_smoke.sh",
    ):
        assert (ROOT / relative).is_file(), relative
