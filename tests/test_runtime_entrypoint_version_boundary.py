from __future__ import annotations

import sonder_runtime.__main__ as entrypoint
from sonder_runtime.platform import version


def test_entrypoint_uses_canonical_version_boundary() -> None:
    assert entrypoint.sonder_version is version
    assert entrypoint.sonder_version.VERSION is version.VERSION
    assert entrypoint.sonder_version.BuildInfo is version.BuildInfo
    assert entrypoint.sonder_version.build_info is version.build_info


def test_entrypoint_build_metadata_matches_canonical_boundary() -> None:
    build = entrypoint.sonder_version.build_info()
    assert build.as_dict() == version.build_info().as_dict()
