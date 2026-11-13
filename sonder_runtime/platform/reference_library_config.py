"""Environment-backed configuration for the reference lookup.

``SONDER_REFERENCE_INDEX`` names the index file; without it the index is
``<state home>/references/references.sqlite`` under the same state home the
runtime policy file uses.  ``SONDER_REFERENCE_LOOKUP=0`` switches the feature
off.  Nothing here creates folders or opens the index.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sonder_runtime.platform.paths import default_home

ENV_INDEX = "SONDER_REFERENCE_INDEX"
ENV_ENABLED = "SONDER_REFERENCE_LOOKUP"
DISABLED_VALUE = "0"
DEFAULT_FOLDER = "references"
DEFAULT_FILE = "references.sqlite"


class IndexPathSource(str, Enum):
    ENVIRONMENT = "environment"
    DEFAULT = "default"


@dataclass(frozen=True)
class ReferenceLibraryConfig:
    enabled: bool
    index_path: Path
    source: IndexPathSource


def lookup_enabled(env: Mapping[str, str] | None = None) -> bool:
    """False only when ``SONDER_REFERENCE_LOOKUP`` is ``0``."""
    values = os.environ if env is None else env
    return str(values.get(ENV_ENABLED, "")).strip() != DISABLED_VALUE


def index_path(env: Mapping[str, str] | None = None) -> tuple[Path, IndexPathSource]:
    """The index file path and where it came from; the path is not checked or created."""
    values = os.environ if env is None else env
    override = str(values.get(ENV_INDEX, "")).strip()
    if override:
        return Path(override).expanduser(), IndexPathSource.ENVIRONMENT
    return default_home() / DEFAULT_FOLDER / DEFAULT_FILE, IndexPathSource.DEFAULT


def load(env: Mapping[str, str] | None = None) -> ReferenceLibraryConfig:
    path, source = index_path(env)
    return ReferenceLibraryConfig(enabled=lookup_enabled(env), index_path=path, source=source)
