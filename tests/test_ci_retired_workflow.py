from pathlib import Path


def test_one_shot_reloadable_recovery_job_is_retired():
    root = Path(__file__).resolve().parents[1]

    assert not (root / ".github" / "workflows" / "restore-reloadable-mcp.yml").exists()
    assert not (root / "scripts" / "apply_loop_docstring_sync.py").exists()
    assert (root / "reloadable_mcp.py").is_file()
