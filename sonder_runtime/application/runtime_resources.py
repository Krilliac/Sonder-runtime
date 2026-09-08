"""Typed ownership accounting for one explicitly composed Application.

This registry is not a handle census. Composition must register every resource
constructor and route its effects through admission before claiming coverage.
Callbacks must prove their own handles closed; a generic close() return is not
proof. The original close observation is immutable even if later cleanup works.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import math
import re
from threading import Condition
from time import monotonic


class ResourceOwnershipRefused(RuntimeError):
    pass


def _name(value):
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise ResourceOwnershipRefused("invalid component identity")
    return value


@dataclass(frozen=True)
class ComponentCloseProof:
    component: str
    closed: bool
    evidence: str

    def __post_init__(self):
        _name(self.component)
        _name(self.evidence)
        if type(self.closed) is not bool:
            raise ResourceOwnershipRefused("exact cleanup proof required")


@dataclass(frozen=True)
class ComponentCloseReceipt:
    component: str
    state: str
    evidence: str


@dataclass(frozen=True)
class ApplicationCloseReceipt:
    manifest_digest: str
    components: tuple[ComponentCloseReceipt, ...]

    @property
    def clean(self):
        return all(item.state in {"UNOPENED", "CLOSED"} for item in self.components)


class ApplicationResourceOwners:
    def __init__(self, components, *, close_order=None):
        if type(components) is not tuple or not 1 <= len(components) <= 64:
            raise ResourceOwnershipRefused("bounded fixed component manifest required")
        names = tuple(sorted(_name(item) for item in components))
        if len(set(names)) != len(names):
            raise ResourceOwnershipRefused("duplicate component identity")
        order = names if close_order is None else close_order
        if type(order) is not tuple or len(order) != len(names) or set(order) != set(names):
            raise ResourceOwnershipRefused("exact component close order required")
        self._close_order = order
        self.manifest_digest = sha256(json.dumps([1, names, order], separators=(",", ":")).encode()).hexdigest()
        self._condition = Condition()
        self._rows = {name: ["UNOPENED", None, None, 0, "never-constructed"] for name in names}
        self._stopped = False
        self._closing = False
        self._receipt = None

    def initialize(self, component, factory, close):
        with self._condition:
            if self._stopped or component not in self._rows:
                raise ResourceOwnershipRefused("component initialization is not admitted")
            row = self._rows[component]
            if row[0] == "ACTIVE":
                return row[1]
            if row[0] != "UNOPENED":
                raise ResourceOwnershipRefused("component initialization is unresolved")
            row[0], row[2] = "INITIALIZING", close
        try:
            resource = factory()
        except BaseException:
            with self._condition:
                row[0], row[4] = "UNRESOLVED", "construction-failed"
                self._condition.notify_all()
            raise
        with self._condition:
            stopped = self._stopped
            row[0], row[1], row[4] = ("CLOSING" if stopped else "ACTIVE"), resource, "cleanup-required"
            self._condition.notify_all()
        if stopped:
            # The constructing thread owns any thread-affine resource. It must
            # attempt cleanup without publishing it to a stopped Application.
            self._close_component(component, row, monotonic())
            raise ResourceOwnershipRefused("component admission stopped during construction")
        return resource

    @contextmanager
    def admission(self, component):
        with self._condition:
            if self._stopped or component not in self._rows or self._rows[component][0] != "ACTIVE":
                raise ResourceOwnershipRefused("component operation is not admitted")
            row = self._rows[component]
            row[3] += 1
        try:
            yield row[1]
        finally:
            with self._condition:
                row[3] -= 1
                self._condition.notify_all()

    def stop_admissions(self):
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def _close_component(self, component, row, deadline):
        try:
            proof = row[2](row[1], max(0, deadline - monotonic()))
            valid = type(proof) is ComponentCloseProof and proof.component == component and proof.closed
            evidence = proof.evidence if type(proof) is ComponentCloseProof else "missing-close-proof"
            if monotonic() > deadline:
                valid, evidence = False, "close-deadline-elapsed"
        except BaseException:
            valid, evidence = False, "close-failed"
        with self._condition:
            row[0], row[4] = ("CLOSED" if valid else "UNRESOLVED"), evidence
            self._condition.notify_all()

    def close(self, *, timeout=5):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ResourceOwnershipRefused("bounded resource close deadline required")
        deadline = monotonic() + timeout
        with self._condition:
            self._stopped = True
            while self._closing or any(row[3] or row[0] in {"INITIALIZING", "CLOSING"} for row in self._rows.values()):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return self._retain_receipt()
                self._condition.wait(remaining)
            self._closing = True
            pending = [(name, self._rows[name]) for name in self._close_order if self._rows[name][0] == "ACTIVE"]
        try:
            for name, row in pending:
                self._close_component(name, row, deadline)
        finally:
            with self._condition:
                self._closing = False
                self._condition.notify_all()
        with self._condition:
            return self._retain_receipt()

    def _retain_receipt(self):
        if self._receipt is None:
            self._receipt = ApplicationCloseReceipt(self.manifest_digest, tuple(
                ComponentCloseReceipt(name, row[0] if row[0] in {"CLOSED", "UNOPENED"} else "UNRESOLVED", row[4])
                for name, row in self._rows.items()
            ))
        return self._receipt
