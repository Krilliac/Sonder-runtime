"""Small non-launching owner profile and eligibility check for real-child tests."""

import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution import runtime_payload
from sonder_runtime.application.ports.runtime_owner import OwnerRefused

TEST_INTEGRATION_MAX_BYTES = 512 * 1024**2


@pytest.fixture
def small_managed_runtime_layout(monkeypatch, tmp_path):
    """Keep owner unit tests independent of the host's training packages.

    Only tests that never execute the child use this profile. The real Windows
    HTTP/PG integration tests continue using the actual interpreter closure.
    """
    source = Path(__file__).resolve().parents[1]
    base = tmp_path / "tiny-interpreter"
    (base / "Lib").mkdir(parents=True)
    (base / "DLLs").mkdir()
    executable = base / "python.exe"
    executable.write_bytes(b"MZ\0test-only-interpreter")
    dependencies = tmp_path / "tiny-env" / "Lib" / "site-packages"
    dependencies.mkdir(parents=True)
    (dependencies / "dependency.py").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        runtime_payload,
        "_runtime_layout",
        lambda runtime_venv=None: (source, base, executable, dependencies, ()),
    )
    return source


def require_bounded_real_runtime_closure():
    """Reserve real-child integration for lean CI/test environments.

    Production's 4 GiB allowance is unchanged. A development venv bloated by
    unrelated training packages would make this test repeat gigabytes of
    hashing; it requires separate dedicated-venv Windows qualification.
    """
    base = Path(sys.base_prefix).resolve()
    dependencies = Path(sys.prefix).resolve() / "Lib" / "site-packages"
    external = [(str(base / "Lib"), True), (str(base / "DLLs"), False),
                (str(dependencies), False)]
    external.extend(
        (str(path), False)
        for path in base.iterdir()
        if path.is_file() and path.suffix.lower() in (".dll", ".exe", ".zip")
    )
    try:
        planned = runtime_payload.preflight(external)
    except OwnerRefused as exc:
        if "verification budget" in str(exc):
            pytest.skip(
                "real managed-child integration needs a dedicated test venv: "
                + str(exc)
            )
        raise
    if sum(metadata.st_size for _, metadata in planned) > TEST_INTEGRATION_MAX_BYTES:
        pytest.skip(
            "real managed-child integration needs a dedicated lean test venv "
            "(production closure limits are unchanged)"
        )
