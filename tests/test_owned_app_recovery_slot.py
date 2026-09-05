"""New recovery registry is bound to the actual isolated Application resource owner."""

import os
from pathlib import Path
import subprocess
import sys
from tests.test_owned_app_work_slot import SCRIPT


def test_exact_owned_recovery_registration_and_shutdown(tmp_path):
    body = """    if mode == 'recovery':
        from sonder_runtime.bootstrap.app_work_recovery_registry import AppWorkRecoveryRegistry
        from sonder_runtime.bootstrap.managed_app_work import register_owned_app_recovery, require_owned_app_recovery
        value = dispatcher(application)
        register_owned_app_work(application, value).commit()
        refused(OwnerUnsupported, lambda: require_owned_app_recovery(application))
        native = NativePool(max_workers=1)
        wrong = AppWorkRecoveryRegistry(application=application, authority=value.authority,
            attempt_factory=lambda *args: None, executor=native)
        refused(OwnerRefused, lambda: register_owned_app_recovery(application, wrong))
        wrong.close()
        registry = AppWorkRecoveryRegistry(application=application, authority=value.authority,
            attempt_factory=lambda *args: None, executor=workers.pool(max_workers=1))
        lease = register_owned_app_recovery(application, registry)
        refused(OwnerRefused, lambda: seal_owned_app_work(application))
        assert require_owned_app_recovery(application) is registry
        lease.commit()
        refused(OwnerRefused, lease.rollback)
        seal_owned_app_work(application)
        assert resources.close(timeout=2).clean
        assert registry._closed
        refused(OwnerRefused, lambda: require_owned_app_recovery(application))
    elif mode == 'identity':
"""
    script = SCRIPT.replace("    if mode == 'identity':\n", body)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "state"), "recovery"],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(
            os.environ, SONDER_HOME=str(tmp_path / "state"), SONDER_OLLAMA_WORKERS=""
        ),
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
