"""Opt-in host composition; no model-visible registration or implicit approval."""

from ..adapters.delegated_verification import CatalogVerificationGateway
from ..adapters.filesystem.workspace_manifest import WorkspaceSnapshotter
from ..adapters.lane_tests import LaneTestCatalog
from ..application.agents.delegated_verification import DelegatedVerificationService


def compose_delegated_verification(
    lanes, process_provider, catalog_path, *, targets=None, snapshotter=None
):
    catalog = LaneTestCatalog.load(catalog_path)
    gateway = CatalogVerificationGateway(catalog, process_provider, targets=targets)
    return DelegatedVerificationService(
        lanes,
        gateway,
        process_provider.cleanup_proof,
        snapshotter or WorkspaceSnapshotter(),
    )
