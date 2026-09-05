"""Offline canonical aggregate transfer; no runner or owner takeover API."""

from collections.abc import Callable, Iterable
from typing import Protocol, TypeVar

T = TypeVar("T")
Record = dict[str, object]
Manifest = dict[str, object]


class ChildMigrationSnapshot(Protocol):
    def metadata(self) -> dict[str, object]: ...
    def records(self, stream: str) -> Iterable[Record]: ...


class ChildMigrationStore(Protocol):
    identity: str

    def read_snapshot(
        self, function: Callable[[ChildMigrationSnapshot], T], *, bundle=None
    ) -> T: ...
    def prepare(self, manifest: Manifest) -> None: ...
    def status(self, manifest: Manifest) -> dict[str, object]: ...
    def copy_page(
        self, manifest: Manifest, stream: str, index: int, records: tuple[Record, ...]
    ) -> None: ...
    def copied(self, manifest: Manifest) -> None: ...
    def verified(self, manifest: Manifest) -> None: ...
