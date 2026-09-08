import json

from sonder_runtime.bootstrap.child_migration import main
from tests.test_child_migration import seed


def test_real_cli_stage_verify_and_explicit_unsupported_activation(
    tmp_path, capsys, monkeypatch
):
    import sonder_runtime.bootstrap.child_migration as module

    monkeypatch.setattr(module, "allowed_roots", lambda: ())
    source, target, bundle = (
        tmp_path / "source.db",
        tmp_path / "target.db",
        tmp_path / "bundle",
    )
    seed(source, 2)
    args = [
        "--source-sqlite",
        str(source),
        "--target-sqlite",
        str(target),
        "--bundle",
        str(bundle),
    ]
    assert main(["export", *args]) == 0
    output = capsys.readouterr().out
    identity = json.loads(output)["migration_id"]
    assert "Unicode" not in output and str(source) not in output
    for action in ("stage", "resume", "verify", "status"):
        assert main([action, *args, "--migration-id", identity]) == 0
        assert json.loads(capsys.readouterr().out)
    assert main(["activate", *args, "--migration-id", identity]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unsupported"
    assert source.is_file()


def test_cli_wrong_identity_has_no_target_effect(tmp_path, capsys, monkeypatch):
    import sonder_runtime.bootstrap.child_migration as module

    monkeypatch.setattr(module, "allowed_roots", lambda: ())
    source, target, bundle = (
        tmp_path / "source.db",
        tmp_path / "target.db",
        tmp_path / "bundle",
    )
    seed(source, 1)
    args = [
        "--source-sqlite",
        str(source),
        "--target-sqlite",
        str(target),
        "--bundle",
        str(bundle),
    ]
    assert main(["export", *args]) == 0
    capsys.readouterr()
    assert main(["stage", *args, "--migration-id", "wrong"]) == 1
    assert not target.exists()
