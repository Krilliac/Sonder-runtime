"""Keep production rollback inside the persisted deployment write set."""
from collections.abc import Mapping, Sequence


def rollback_manifest(manifest: Mapping, deployed_paths: Sequence[str]) -> dict:
    """Select verified backups without rewriting approved, unchanged siblings.

    Callers first verify the complete sealed backup. The durable deployed
    inventory (or tested write set before inventory publication) selects the
    only files rollback may restore. Missing scope refuses before live writes.
    """
    paths = set(deployed_paths)
    records = [record for record in manifest["files"] if record["path"] in paths]
    if not paths or {record["path"] for record in records} != paths:
        raise RuntimeError("rollback requires a persisted write set with verified backups")
    return dict(manifest, files=records)
