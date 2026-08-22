"""Pure, bounded ownership records for the ARCH-012 architecture map.

The catalog is deliberately an in-memory value object.  It does not discover
packages, inspect the filesystem, or persist its result; composition and
generation layers may supply those concerns later.  This module only makes
ownership data typed, complete, bounded, and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


_MAX_ENTRIES = 512
_MAX_TEXT = 160


class OwnershipCatalogError(ValueError):
    """Base error for invalid ownership data."""


class OwnershipValidationError(OwnershipCatalogError):
    """Raised when an ownership record is incomplete or ambiguous."""


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OwnershipValidationError(f"{field} is required")
    value = value.strip()
    if len(value) > _MAX_TEXT:
        raise OwnershipValidationError(f"{field} exceeds {_MAX_TEXT} characters")
    return value


@dataclass(frozen=True, slots=True)
class PackageOwnership:
    """The package that is the unit of ownership accounting."""

    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "package name"))


@dataclass(frozen=True, slots=True)
class StateOwnership:
    """A named state surface and the owner responsible for its lifecycle."""

    name: str
    owner: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "state name"))
        object.__setattr__(self, "owner", _text(self.owner, "state owner"))


@dataclass(frozen=True, slots=True)
class PublicPortOwnership:
    """A public application port and its owning package/component."""

    name: str
    owner: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "public port name"))
        object.__setattr__(self, "owner", _text(self.owner, "public port owner"))


@dataclass(frozen=True, slots=True)
class ProviderOwnership:
    """An infrastructure/provider boundary and its owner."""

    name: str
    owner: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "provider name"))
        object.__setattr__(self, "owner", _text(self.owner, "provider owner"))


@dataclass(frozen=True, slots=True)
class SchemaOwnership:
    """A persisted or wire schema and its owner."""

    name: str
    owner: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "schema name"))
        object.__setattr__(self, "owner", _text(self.owner, "schema owner"))


@dataclass(frozen=True, slots=True)
class LifecycleOwnership:
    """A lifecycle responsibility and the owner accountable for it."""

    name: str
    owner: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "lifecycle name"))
        object.__setattr__(self, "owner", _text(self.owner, "lifecycle owner"))


@dataclass(frozen=True, slots=True)
class OwnershipRecord:
    """The complete ARCH-012 ownership row for one package."""

    package: PackageOwnership
    state: StateOwnership
    public_port: PublicPortOwnership
    provider: ProviderOwnership
    schema: SchemaOwnership
    lifecycle: LifecycleOwnership

    def __post_init__(self) -> None:
        fields = (
            ("package", self.package, PackageOwnership),
            ("state", self.state, StateOwnership),
            ("public_port", self.public_port, PublicPortOwnership),
            ("provider", self.provider, ProviderOwnership),
            ("schema", self.schema, SchemaOwnership),
            ("lifecycle", self.lifecycle, LifecycleOwnership),
        )
        for field, value, expected in fields:
            if not isinstance(value, expected):
                raise OwnershipValidationError(f"{field} ownership is required")


class OwnershipCatalog:
    """A deterministic, validated collection of complete ownership rows.

    Names are unique within each ownership category.  This prevents two
    packages from claiming the same state, port, provider, schema, or
    lifecycle responsibility while still allowing one owner to own multiple
    distinct resources.
    """

    __slots__ = ("_records",)

    def __init__(self, records: Iterable[OwnershipRecord] = ()) -> None:
        if isinstance(records, (str, bytes)):
            raise OwnershipValidationError("records must be ownership records")
        materialized = tuple(records)
        if len(materialized) > _MAX_ENTRIES:
            raise OwnershipValidationError(f"catalog exceeds {_MAX_ENTRIES} entries")
        self._records = self._validate(materialized)

    @staticmethod
    def _validate(records: tuple[OwnershipRecord, ...]) -> tuple[OwnershipRecord, ...]:
        seen: dict[str, set[str]] = {
            "package": set(),
            "state": set(),
            "public_port": set(),
            "provider": set(),
            "schema": set(),
            "lifecycle": set(),
        }
        for index, record in enumerate(records):
            if not isinstance(record, OwnershipRecord):
                raise OwnershipValidationError(f"record {index} is required")
            values = (
                ("package", record.package.name),
                ("state", record.state.name),
                ("public_port", record.public_port.name),
                ("provider", record.provider.name),
                ("schema", record.schema.name),
                ("lifecycle", record.lifecycle.name),
            )
            for field, name in values:
                if name in seen[field]:
                    raise OwnershipValidationError(
                        f"duplicate {field} ownership field: {name}"
                    )
                seen[field].add(name)
        return records

    @property
    def records(self) -> tuple[OwnershipRecord, ...]:
        """Return the immutable records in canonical order."""

        return self._records

    def validate(self) -> None:
        """Revalidate the immutable catalog, raising on invalid state."""

        self._validate(self._records)

    def snapshot(self) -> tuple[dict[str, dict[str, str]], ...]:
        """Return a stable, machine-readable snapshot independent of input order."""

        rows = []
        for record in self._records:
            rows.append(
                {
                    "package": {"name": record.package.name},
                    "state": {"name": record.state.name, "owner": record.state.owner},
                    "public_port": {
                        "name": record.public_port.name,
                        "owner": record.public_port.owner,
                    },
                    "provider": {
                        "name": record.provider.name,
                        "owner": record.provider.owner,
                    },
                    "schema": {"name": record.schema.name, "owner": record.schema.owner},
                    "lifecycle": {
                        "name": record.lifecycle.name,
                        "owner": record.lifecycle.owner,
                    },
                }
            )
        return tuple(sorted(rows, key=lambda row: row["package"]["name"]))


def default_layer_ownership_catalog(
    layers: Iterable[str] = ("domain", "application", "adapters", "interfaces", "platform", "bootstrap"),
) -> OwnershipCatalog:
    """Build the deterministic layer-level inventory used by generated maps.

    This is intentionally a composition projection, not filesystem discovery:
    callers supply the layer names from the authoritative package map and the
    catalog supplies one complete ownership row for each.  Resource-level
    ownership can be added later without changing the record shape.
    """
    names = tuple(layers)
    if not names or any(not isinstance(name, str) or not name.strip() for name in names):
        raise OwnershipValidationError("ownership layers must be non-empty names")
    if len(set(names)) != len(names):
        raise OwnershipValidationError("ownership layers must be unique")
    records = []
    for layer in sorted(name.strip() for name in names):
        owner = f"sonder_runtime.{layer}"
        records.append(
            OwnershipRecord(
                package=PackageOwnership(owner),
                state=StateOwnership(f"{layer}.state", owner),
                public_port=PublicPortOwnership(f"{layer}.ports", owner),
                provider=ProviderOwnership(f"{layer}.providers", owner),
                schema=SchemaOwnership(f"{layer}.schemas", owner),
                lifecycle=LifecycleOwnership(f"{layer}.lifecycle", owner),
            )
        )
    return OwnershipCatalog(records)


__all__ = [
    "LifecycleOwnership",
    "OwnershipCatalog",
    "OwnershipCatalogError",
    "OwnershipRecord",
    "OwnershipValidationError",
    "PackageOwnership",
    "ProviderOwnership",
    "PublicPortOwnership",
    "SchemaOwnership",
    "StateOwnership",
    "default_layer_ownership_catalog",
]
