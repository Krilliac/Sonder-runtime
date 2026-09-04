"""Bloom filter for probabilistic set membership.

A space-efficient probabilistic data structure that can tell you
"definitely not in set" or "probably in set".  False positives are
possible; false negatives are not.

Includes a sliding-window variant that expires old entries, useful for
time-bounded deduplication of fleet task submissions.

No I/O, no threading -- callers own synchronization.
"""
from __future__ import annotations

import hashlib
import math
import struct
import time
from dataclasses import dataclass


def _optimal_params(expected_items: int, fp_rate: float) -> tuple[int, int]:
    if expected_items <= 0:
        raise ValueError("expected_items must be positive")
    if not (0.0 < fp_rate < 1.0):
        raise ValueError("fp_rate must be between 0 and 1")
    m = -expected_items * math.log(fp_rate) / (math.log(2) ** 2)
    k = (m / expected_items) * math.log(2)
    return max(64, int(math.ceil(m))), max(1, int(math.ceil(k)))


def _hash_positions(key: str, num_bits: int, num_hashes: int) -> list[int]:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    h1 = struct.unpack(">Q", digest[:8])[0]
    h2 = struct.unpack(">Q", digest[8:16])[0]
    return [(h1 + i * h2) % num_bits for i in range(num_hashes)]


class BloomFilter:
    """Fixed-size Bloom filter."""

    def __init__(
        self,
        expected_items: int = 1000,
        fp_rate: float = 0.01,
    ) -> None:
        self._num_bits, self._num_hashes = _optimal_params(expected_items, fp_rate)
        self._bits = bytearray(self._num_bits // 8 + 1)
        self._count = 0

    def add(self, key: str) -> bool:
        positions = _hash_positions(key, self._num_bits, self._num_hashes)
        already = all(self._get_bit(p) for p in positions)
        for p in positions:
            self._set_bit(p)
        if not already:
            self._count += 1
        return not already

    def contains(self, key: str) -> bool:
        positions = _hash_positions(key, self._num_bits, self._num_hashes)
        return all(self._get_bit(p) for p in positions)

    def _set_bit(self, pos: int) -> None:
        self._bits[pos >> 3] |= 1 << (pos & 7)

    def _get_bit(self, pos: int) -> bool:
        return bool(self._bits[pos >> 3] & (1 << (pos & 7)))

    @property
    def count(self) -> int:
        return self._count

    @property
    def num_bits(self) -> int:
        return self._num_bits

    @property
    def num_hashes(self) -> int:
        return self._num_hashes

    def estimated_fp_rate(self) -> float:
        if self._count == 0:
            return 0.0
        bits_set = sum(bin(b).count("1") for b in self._bits)
        fraction = bits_set / self._num_bits
        return fraction ** self._num_hashes


@dataclass
class _WindowEntry:
    key: str
    ts: float


class SlidingBloomFilter:
    """Time-windowed Bloom filter that expires old entries.

    Maintains two internal filters and rotates them at half the
    window interval, so entries older than ``window_seconds`` are
    eventually dropped while maintaining the "no false negatives
    within the window" guarantee.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        expected_items: int = 1000,
        fp_rate: float = 0.01,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._window = window_seconds
        self._expected = expected_items
        self._fp_rate = fp_rate
        self._current = BloomFilter(expected_items, fp_rate)
        self._previous = BloomFilter(expected_items, fp_rate)
        self._last_rotation: float | None = None
        self._total_added = 0

    def add(self, key: str, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.monotonic()
        self._maybe_rotate(ts)
        was_new = self._current.add(key)
        if was_new:
            self._total_added += 1
        return was_new

    def contains(self, key: str, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.monotonic()
        self._maybe_rotate(ts)
        return self._current.contains(key) or self._previous.contains(key)

    def _maybe_rotate(self, now: float) -> None:
        if self._last_rotation is None:
            self._last_rotation = now
            return
        if now - self._last_rotation >= self._window / 2:
            self._previous = self._current
            self._current = BloomFilter(self._expected, self._fp_rate)
            self._last_rotation = now

    @property
    def total_added(self) -> int:
        return self._total_added
