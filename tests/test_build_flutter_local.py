from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_flutter_builder_uses_canonical_sonder_identity():
    text = (ROOT / "scripts" / "build_flutter_local.ps1").read_text(
        encoding="utf-8"
    )

    assert "--org com.sonder.runtime" in text
    assert "--project-name sonder_runtime" in text
    assert "configure_flutter_networking.py" in text
    assert "sonder.exe" in text
    assert "sonder-runtime-android.apk" in text
