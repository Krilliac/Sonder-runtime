"""Bounded app work data. These records do not confer execution authority."""

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path

from .app_control import digest as require_digest


def canonical_digest(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _text(value, maximum, *, multiline=False):
    if type(value) is not str:
        raise ValueError("bounded work text required")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("invalid work text encoding") from None
    if not 1 <= size <= maximum or any(
        (ord(char) < 32 and not (multiline and char in "\n\r\t")) or ord(char) == 127
        for char in value
    ):
        raise ValueError("invalid work text")


@dataclass(frozen=True)
class WorkSpec:
    """Exact task and resolved execution options, supplied by trusted preparation.

    Prompt whitespace is meaningful and is never normalized before hashing.
    A model identifier records the resolved choice; it is not a capability or
    permission to use a cloud provider. Live composition enforces that policy.
    """

    prompt: str = field(repr=False)
    tier: str
    resolved_model: str
    max_steps: int
    allow_web: bool

    def __post_init__(self):
        _text(self.prompt, 16384, multiline=True)
        _text(self.tier, 128)
        _text(self.resolved_model, 512)
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 64:
            raise ValueError("bounded work step count required")
        if type(self.allow_web) is not bool:
            raise ValueError("explicit work web policy required")

    @property
    def digest(self):
        return canonical_digest({"schema": 1, "spec": asdict(self)})


@dataclass(frozen=True)
class PreparedWorkbenchRun:
    """Host-selected immutable model plan; execution must revalidate its policy."""

    spec: WorkSpec
    project_root: str = field(repr=False)
    model_ladder: tuple[str, ...]
    policy_digest: str
    allow_location: bool

    def __post_init__(self):
        if type(self.spec) is not WorkSpec:
            raise ValueError("typed work specification required")
        self.spec.__post_init__()
        _text(self.project_root, 4096)
        root = Path(self.project_root)
        if not root.is_absolute() or str(root.resolve()) != self.project_root or not root.is_dir():
            raise ValueError("canonical existing work root required")
        if type(self.model_ladder) is not tuple or not 1 <= len(self.model_ladder) <= 8:
            raise ValueError("bounded prepared model ladder required")
        for model in self.model_ladder:
            _text(model, 512)
        if len(set(self.model_ladder)) != len(self.model_ladder) or self.model_ladder[0] != self.spec.resolved_model:
            raise ValueError("resolved model must lead a unique prepared ladder")
        require_digest(self.policy_digest)
        if type(self.allow_location) is not bool:
            raise ValueError("explicit location policy required")

    @property
    def digest(self):
        return canonical_digest({"schema": 1, "plan": asdict(self)})
