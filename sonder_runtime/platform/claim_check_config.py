"""Bounded windows and limits for the deterministic claim checker."""
from dataclasses import dataclass
import re

_MAX_PATTERNS = 32
_MAX_PATTERN_CHARS = 200
_DIR_NAME = re.compile(r"[^\/:*?\"<>|\r\n]{1,128}")


@dataclass(frozen=True)
class ClaimCheckConfig:
    # Lines either side of the cited range searched for a named symbol.
    symbol_window_lines: int = 10
    # Lines either side of the cited range (and of each caller) searched for a guard.
    guard_window_lines: int = 15
    caller_window_lines: int = 8
    # Claims on the same file whose cited lines are this close collapse into one.
    duplicate_window_lines: int = 10
    max_file_bytes: int = 2_000_000
    max_callers: int = 10
    max_caller_files: int = 2000
    max_cache_bytes: int = 64_000_000
    max_evidence: int = 20
    skip_dirs: tuple[str, ...] = (".git", "node_modules", "bin", "obj", ".vs", "__pycache__")
    # Extra regular expressions counted as guard shapes for every category.
    extra_guard_patterns: tuple[str, ...] = ()
    # "all": every named symbol must be near the cited line; "any": one is enough.
    symbol_match: str = "all"


_INT_BOUNDS = (
    ("symbol_window_lines", 0, 200), ("guard_window_lines", 0, 200),
    ("caller_window_lines", 0, 200), ("duplicate_window_lines", 0, 200),
    ("max_file_bytes", 1024, 16_000_000), ("max_callers", 0, 100),
    ("max_caller_files", 0, 50_000), ("max_cache_bytes", 1024, 512_000_000),
    ("max_evidence", 1, 100),
)


def claim_check_errors(config) -> list[str]:
    section = config.claim_check
    errors = []
    for name, low, high in _INT_BOUNDS:
        value = getattr(section, name)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"[claim_check].{name} must be an integer in {low}..{high}")
    if section.symbol_match not in ("all", "any"):
        errors.append("[claim_check].symbol_match must be all or any")
    dirs = section.skip_dirs
    if (type(dirs) is not tuple or len(dirs) > 64 or len(set(dirs)) != len(dirs)
            or any(type(v) is not str or not _DIR_NAME.fullmatch(v) for v in dirs)):
        errors.append("[claim_check].skip_dirs must be up to 64 distinct directory names")
    patterns = section.extra_guard_patterns
    if (type(patterns) is not tuple or len(patterns) > _MAX_PATTERNS
            or any(type(v) is not str or not 1 <= len(v) <= _MAX_PATTERN_CHARS for v in patterns)):
        errors.append(
            f"[claim_check].extra_guard_patterns must be up to {_MAX_PATTERNS} "
            f"patterns of 1..{_MAX_PATTERN_CHARS} characters")
    else:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"[claim_check].extra_guard_patterns has an invalid regex: {exc}")
                break
    return errors
