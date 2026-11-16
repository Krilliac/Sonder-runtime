"""Bounded JSONL dataset loader: the only place the dataset file is read."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from ...domain.security_eval.errors import DatasetError
from ...domain.security_eval.records import HypothesisRecord, collect_records


@dataclass(frozen=True)
class DatasetLoad:
    records: tuple[HypothesisRecord, ...]
    invalid: tuple[dict, ...]
    sha256: str
    path: str


def load_dataset(path: str, *, max_bytes: int, max_records: int) -> DatasetLoad:
    """Read ``path`` (UTF-8 JSONL) and return the valid records plus the invalid-line report.

    Raises ``DatasetError`` for a missing, oversize or unreadable file, more than ``max_records``
    non-blank lines, or no valid record. The file is only read, never written.
    """
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise DatasetError("no dataset path was given (argument or SONDER_SECURITY_EVAL_DATASET)")
    target = os.path.abspath(path.strip())
    try:
        size = os.stat(target).st_size
        if not os.path.isfile(target):
            raise DatasetError("the dataset path is not a file")
        if size > max_bytes:
            raise DatasetError("the dataset is %d bytes, over the %d byte limit" % (size, max_bytes))
        with open(target, "rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise DatasetError("the dataset could not be read (%s)" % type(exc).__name__) from None
    if len(data) > max_bytes:
        raise DatasetError("the dataset is over the %d byte limit" % max_bytes)
    text = data.decode("utf-8-sig", errors="replace")
    numbered = [(number, line) for number, line in enumerate(text.splitlines(), start=1)]
    if sum(1 for _n, line in numbered if line.strip()) > max_records:
        raise DatasetError("the dataset has more than %d records" % max_records)
    records, invalid = collect_records(numbered)
    return DatasetLoad(tuple(records), tuple(invalid), hashlib.sha256(data).hexdigest(), target)
