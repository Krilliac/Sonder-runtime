"""Shared, bounded contract for Sonder chat-training JSONL snapshots."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path


# These limits fit the attended local-training target.  In particular, the
# aggregate character limit bounds the much larger Python token-list expansion
# that occurs before Hugging Face converts examples into an Arrow dataset.
MAX_TRAINING_FILE_BYTES = 16 * 1024 * 1024
MAX_TRAINING_RECORD_BYTES = 64 * 1024
MAX_TRAINING_FIELD_CHARS = 32 * 1024
MAX_TRAINING_TOTAL_CHARS = 4 * 1024 * 1024
MAX_TRAINING_EXAMPLES = 10_000


class TrainingDataError(RuntimeError):
    """The dataset is malformed, unsupported, or outside reviewed bounds."""


class _DuplicateKey(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    file_bytes: int = MAX_TRAINING_FILE_BYTES
    record_bytes: int = MAX_TRAINING_RECORD_BYTES
    field_chars: int = MAX_TRAINING_FIELD_CHARS
    total_chars: int = MAX_TRAINING_TOTAL_CHARS
    examples: int = MAX_TRAINING_EXAMPLES


@dataclass(frozen=True)
class Inspection:
    examples: list[dict]
    sha256: str
    file_bytes: int
    content_chars: int


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey("duplicate JSON object key")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _normalize_surrogates(value):
    """Combine valid JSON surrogate pairs and reject only unpaired halves."""
    normalized = []
    index = 0
    while index < len(value):
        codepoint = ord(value[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(value):
                raise TrainingDataError("message content contains invalid Unicode")
            low = ord(value[index + 1])
            if not 0xDC00 <= low <= 0xDFFF:
                raise TrainingDataError("message content contains invalid Unicode")
            normalized.append(
                chr(0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00))
            )
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            raise TrainingDataError("message content contains invalid Unicode")
        normalized.append(value[index])
        index += 1
    return "".join(normalized)


def canonical_record(record, *, line_number=0, limits=Limits()):
    """Validate and reconstruct one exact user/assistant training record."""
    label = "training data line %d" % line_number if line_number else "training record"
    if not isinstance(record, dict):
        raise TrainingDataError("%s must be a JSON object" % label)
    if set(record) != {"messages"}:
        raise TrainingDataError("%s must contain only the messages field" % label)
    messages = record["messages"]
    if not isinstance(messages, list) or len(messages) != 2:
        raise TrainingDataError("%s must contain exactly two messages" % label)

    canonical = []
    for index, expected_role in enumerate(("user", "assistant"), start=1):
        message = messages[index - 1]
        if not isinstance(message, dict):
            raise TrainingDataError("%s message %d must be an object" % (label, index))
        if set(message) != {"role", "content"}:
            raise TrainingDataError(
                "%s message %d must contain only role and content" % (label, index)
            )
        if message["role"] != expected_role:
            raise TrainingDataError(
                "%s messages must be ordered user then assistant" % label
            )
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise TrainingDataError("%s message content must be non-empty text" % label)
        if len(content) > limits.field_chars:
            raise TrainingDataError(
                "%s message content exceeds the supported field size limit" % label
            )
        try:
            content = _normalize_surrogates(content)
        except TrainingDataError as exc:
            raise TrainingDataError(
                "%s message content contains invalid Unicode" % label
            ) from exc
        canonical.append({"role": expected_role, "content": content})
    return {"messages": canonical}


def _parse_record(text, *, line_number, limits):
    try:
        record = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError) as exc:
        raise TrainingDataError(
            "training data line %d is not valid strict JSON" % line_number
        ) from exc
    return canonical_record(record, line_number=line_number, limits=limits)


def inspect_jsonl(path, expected_sha256="", *, limits=Limits()):
    """Read, hash, and validate the exact bytes from one open file handle."""
    examples = []
    digest = hashlib.sha256()
    total_bytes = 0
    total_chars = 0
    with Path(path).open("rb") as stream:
        if os.fstat(stream.fileno()).st_size > limits.file_bytes:
            raise TrainingDataError("training data file exceeds the supported size limit")
        line_number = 0
        while True:
            raw_line = stream.readline(limits.record_bytes + 1)
            if not raw_line:
                break
            line_number += 1
            total_bytes += len(raw_line)
            digest.update(raw_line)
            if total_bytes > limits.file_bytes:
                raise TrainingDataError("training data file exceeds the supported size limit")
            if len(raw_line) > limits.record_bytes:
                raise TrainingDataError(
                    "training data line %d exceeds the supported record size limit"
                    % line_number
                )
            payload = raw_line[:-1] if raw_line.endswith(b"\n") else raw_line
            if payload.endswith(b"\r"):
                payload = payload[:-1]
            if not payload:
                raise TrainingDataError("training data line %d is empty" % line_number)
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TrainingDataError(
                    "training data line %d is not valid UTF-8" % line_number
                ) from exc
            record = _parse_record(text, line_number=line_number, limits=limits)
            total_chars += sum(
                len(message["content"]) for message in record["messages"]
            )
            if total_chars > limits.total_chars:
                raise TrainingDataError(
                    "training data exceeds the supported aggregate content limit"
                )
            examples.append(record)
            if len(examples) > limits.examples:
                raise TrainingDataError("training data contains too many examples")

    actual = digest.hexdigest()
    if expected_sha256 and not hmac.compare_digest(actual, str(expected_sha256)):
        raise TrainingDataError("training data changed while loading the authorized snapshot")
    return Inspection(examples, actual, total_bytes, total_chars)


def encode_jsonl(examples, *, limits=Limits()):
    """Validate and serialize examples without exceeding the shared bounds."""
    payload = bytearray()
    total_chars = 0
    count = 0
    for count, record in enumerate(examples, start=1):
        if count > limits.examples:
            raise TrainingDataError("training data contains too many examples")
        canonical = canonical_record(record, line_number=count, limits=limits)
        total_chars += sum(
            len(message["content"]) for message in canonical["messages"]
        )
        if total_chars > limits.total_chars:
            raise TrainingDataError(
                "training data exceeds the supported aggregate content limit"
            )
        encoded = (
            json.dumps(canonical, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > limits.record_bytes:
            raise TrainingDataError(
                "training data line %d exceeds the supported record size limit" % count
            )
        if len(payload) + len(encoded) > limits.file_bytes:
            raise TrainingDataError("training data file exceeds the supported size limit")
        payload.extend(encoded)
    return bytes(payload)
