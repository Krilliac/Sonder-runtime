"""Immutable host lifecycle identities; never account or model capabilities."""
from dataclasses import dataclass
import hashlib
import json
import re

from .runtime_owner import OwnerRefused, canonical


def _number(value):
    if type(value) is not int or not 0 <= value < 2**62:
        raise OwnerRefused("invalid managed owner generation")


def config_reference(value):
    if type(value) is not dict or set(value) != {"generation", "digest", "selector_revision"}:
        raise OwnerRefused("exact immutable config reference required")
    _number(value["generation"])
    _number(value["selector_revision"])
    if value["generation"] == 0 or type(value["digest"]) is not str or re.fullmatch(r"[a-f0-9]{64}", value["digest"]) is None:
        raise OwnerRefused("invalid immutable config reference")
    return value


@dataclass(frozen=True)
class PreparedManagedOwnerOperation:
    operation_id: str
    action: str
    namespace: str
    incarnation: str
    expected_revision: int
    epoch: int
    config_revision: int
    selector_revision: int
    payload: bytes

    def __post_init__(self):
        if type(self.operation_id) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.operation_id) is None:
            raise OwnerRefused("invalid managed owner operation ID")
        if type(self.namespace) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.namespace) is None:
            raise OwnerRefused("invalid managed owner namespace")
        if type(self.incarnation) is not str or re.fullmatch(r"[a-f0-9]{32}", self.incarnation) is None:
            raise OwnerRefused("invalid managed owner incarnation")
        for value in (self.expected_revision, self.epoch, self.config_revision, self.selector_revision):
            _number(value)
        if self.epoch == 0 or self.action not in {"select", "launch", "stop", "activate"}:
            raise OwnerRefused("unsupported managed owner action or epoch")
        if type(self.payload) is not bytes or len(self.payload) > 32768:
            raise OwnerRefused("managed owner payload exceeds bounds")
        try:
            value = json.loads(self.payload)
            if type(value) is not dict or canonical(value) != self.payload:
                raise ValueError()
            keys = {"select": {"config"}, "launch": set(), "stop": set(), "activate": {"manifest_digest", "target"}}
            if set(value) != keys[self.action]:
                raise ValueError()
            if self.action == "select":
                config_reference(value["config"])
            if self.action == "activate":
                config_reference(value["target"])
                if type(value["manifest_digest"]) is not str or re.fullmatch(r"[a-f0-9]{64}", value["manifest_digest"]) is None:
                    raise ValueError()
        except (ValueError, TypeError, RecursionError):
            raise OwnerRefused("invalid immutable managed owner payload") from None

    @property
    def digest(self):
        return hashlib.sha256(canonical([self.operation_id, self.action, self.namespace, self.incarnation, self.expected_revision, self.epoch, self.config_revision, self.selector_revision, self.payload.decode()])).hexdigest()


def managed_operation(operation_id, action, status, arguments):
    return PreparedManagedOwnerOperation(operation_id, action, status["namespace"], status["incarnation"], status["revision"], status["epoch"], status["config_revision"], status["selector_revision"], canonical(arguments))
