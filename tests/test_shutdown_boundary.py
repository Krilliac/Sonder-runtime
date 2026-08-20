"""Packaged shutdown-boundary regression coverage."""
from __future__ import annotations

import sonder_runtime.adapters.web.lifecycle as lifecycle
import sonder_shutdown
from sonder_runtime.platform.shutdown import CancellationToken
from sonder_runtime.platform.shutdown import ShutdownCoordinator


def test_packaged_lifecycle_uses_packaged_shutdown_boundary():
    assert lifecycle.ShutdownCoordinator is ShutdownCoordinator


def test_root_shutdown_is_identity_preserving_compatibility_shim():
    assert sonder_shutdown.ShutdownCoordinator is ShutdownCoordinator
    assert sonder_shutdown.CancellationToken is CancellationToken


def test_cancellation_token_preserves_cooperative_contract():
    token = CancellationToken()
    assert token.cancelled is False
    assert token.wait(0) is False
    token.cancel()
    assert token.cancelled is True
    assert token.wait(0) is True
