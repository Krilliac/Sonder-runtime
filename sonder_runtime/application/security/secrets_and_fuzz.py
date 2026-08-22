"""Bounded secret detection and decoder-fuzzing contracts (WP9)."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Iterable


@dataclass(frozen=True)
class CredentialFinding:
    kind: str
    line: int
    redacted: str


class CredentialScanner:
    """Detect common credential-shaped material without returning the secret."""

    _patterns = (
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
        ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
        ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
        ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
        ("credential_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]+")),
    )

    def __init__(self, *, max_bytes: int = 1_000_000) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def scan(self, content: str | bytes) -> tuple[CredentialFinding, ...]:
        raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        if len(raw.encode("utf-8")) > self.max_bytes:
            raise ValueError("content exceeds scan bound")
        findings: list[CredentialFinding] = []
        for line_no, line in enumerate(raw.splitlines(), 1):
            for kind, pattern in self._patterns:
                if pattern.search(line):
                    findings.append(CredentialFinding(kind, line_no, self._redact(line)))
        return tuple(findings)

    @staticmethod
    def _redact(line: str) -> str:
        # Findings are safe to persist or transmit: do not retain prefixes,
        # suffixes, or labels that can disclose credential material.
        return "[REDACTED CREDENTIAL]"


@dataclass(frozen=True)
class FuzzReport:
    cases: int
    rejected: int
    failures: tuple[str, ...]


class DecoderFuzzHarness:
    """A bounded contract for exercising a decoder with arbitrary byte inputs."""

    def __init__(self, decoder: Callable[[bytes], object], *, max_cases: int = 256, max_input_bytes: int = 4096) -> None:
        if max_cases <= 0 or max_input_bytes <= 0:
            raise ValueError("fuzz bounds must be positive")
        self.decoder = decoder
        self.max_cases = max_cases
        self.max_input_bytes = max_input_bytes

    def run(self, inputs: Iterable[bytes]) -> FuzzReport:
        cases = rejected = 0
        failures: list[str] = []
        for payload in inputs:
            if cases >= self.max_cases:
                break
            cases += 1
            if not isinstance(payload, (bytes, bytearray)) or len(payload) > self.max_input_bytes:
                rejected += 1
                continue
            try:
                self.decoder(bytes(payload))
            except Exception as exc:  # The harness records decoder faults; it must not crash the campaign.
                failures.append(type(exc).__name__)
        return FuzzReport(cases, rejected, tuple(failures))
