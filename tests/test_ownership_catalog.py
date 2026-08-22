"""Focused ARCH-012 ownership catalog contract tests."""

from dataclasses import FrozenInstanceError

import pytest

from sonder_runtime.application.architecture.ownership_catalog import (
    LifecycleOwnership,
    OwnershipCatalog,
    OwnershipRecord,
    OwnershipValidationError,
    PackageOwnership,
    ProviderOwnership,
    PublicPortOwnership,
    SchemaOwnership,
    StateOwnership,
    default_layer_ownership_catalog,
)


def row(package: str = "chat", suffix: str = "chat") -> OwnershipRecord:
    return OwnershipRecord(
        package=PackageOwnership(package),
        state=StateOwnership(f"{suffix}.state", f"{suffix}.state-owner"),
        public_port=PublicPortOwnership(f"{suffix}.port", f"{suffix}.port-owner"),
        provider=ProviderOwnership(f"{suffix}.provider", f"{suffix}.provider-owner"),
        schema=SchemaOwnership(f"{suffix}.schema", f"{suffix}.schema-owner"),
        lifecycle=LifecycleOwnership(f"{suffix}.lifecycle", f"{suffix}.lifecycle-owner"),
    )


def test_snapshot_is_complete_and_order_independent():
    first = OwnershipCatalog((row("zeta", "z"), row("alpha", "a")))
    second = OwnershipCatalog((row("alpha", "a"), row("zeta", "z")))

    assert first.snapshot() == second.snapshot()
    assert first.snapshot()[0]["package"] == {"name": "alpha"}
    assert first.snapshot()[0]["state"] == {"name": "a.state", "owner": "a.state-owner"}
    first.validate()


def test_records_are_typed_and_immutable():
    catalog = OwnershipCatalog((row(),))

    assert catalog.records == (row(),)
    with pytest.raises(FrozenInstanceError):
        catalog.records[0].package = PackageOwnership("other")


@pytest.mark.parametrize(
    "field",
    ("package", "state", "public_port", "provider", "schema", "lifecycle"),
)
def test_missing_ownership_field_is_rejected(field):
    values = {
        "package": PackageOwnership("chat"),
        "state": StateOwnership("chat.state", "owner"),
        "public_port": PublicPortOwnership("chat.port", "owner"),
        "provider": ProviderOwnership("chat.provider", "owner"),
        "schema": SchemaOwnership("chat.schema", "owner"),
        "lifecycle": LifecycleOwnership("chat.lifecycle", "owner"),
    }
    values[field] = None

    with pytest.raises(OwnershipValidationError, match="is required"):
        OwnershipRecord(**values)


@pytest.mark.parametrize("field", ("package", "state", "public_port", "provider", "schema", "lifecycle"))
def test_duplicate_ownership_field_is_rejected(field):
    first = row("chat", "chat")
    second = row("other", "other")
    duplicate_name = getattr(first, field).name
    original = getattr(second, field)
    replacement = type(original)(duplicate_name, original.owner) if field != "package" else PackageOwnership(duplicate_name)
    second = OwnershipRecord(
        package=replacement if field == "package" else second.package,
        state=replacement if field == "state" else second.state,
        public_port=replacement if field == "public_port" else second.public_port,
        provider=replacement if field == "provider" else second.provider,
        schema=replacement if field == "schema" else second.schema,
        lifecycle=replacement if field == "lifecycle" else second.lifecycle,
    )

    with pytest.raises(OwnershipValidationError, match="duplicate"):
        OwnershipCatalog((first, second))


def test_blank_and_oversized_values_are_rejected():
    with pytest.raises(OwnershipValidationError, match="required"):
        PackageOwnership("   ")
    with pytest.raises(OwnershipValidationError, match="exceeds"):
        ProviderOwnership("x" * 161, "owner")


def test_catalog_entry_and_count_are_bounded():
    with pytest.raises(OwnershipValidationError, match="record 0"):
        OwnershipCatalog((object(),))
    with pytest.raises(OwnershipValidationError, match="exceeds"):
        OwnershipCatalog(row(f"package-{index}", f"suffix-{index}") for index in range(513))


def test_default_layer_catalog_is_complete_and_deterministic():
    catalog = default_layer_ownership_catalog(("platform", "domain", "application"))

    assert [row["package"]["name"] for row in catalog.snapshot()] == [
        "sonder_runtime.application", "sonder_runtime.domain", "sonder_runtime.platform",
    ]
    assert catalog.snapshot()[0]["lifecycle"] == {
        "name": "application.lifecycle", "owner": "sonder_runtime.application",
    }


def test_default_layer_catalog_rejects_duplicate_or_blank_names():
    with pytest.raises(OwnershipValidationError, match="unique"):
        default_layer_ownership_catalog(("domain", "domain"))
    with pytest.raises(OwnershipValidationError, match="non-empty"):
        default_layer_ownership_catalog(("domain", " "))
