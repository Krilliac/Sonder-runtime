import sonder_hardware
from sonder_runtime.platform.hardware_probe import parse_memory_gb


def test_hardware_memory_parser_has_canonical_platform_owner():
    assert sonder_hardware._parse_memory_gb is parse_memory_gb


def test_hardware_memory_parser_accepts_decimal_units_and_commas():
    assert parse_memory_gb("16 GB") == 16.0
    assert parse_memory_gb("512 MB") == 0.5
    assert parse_memory_gb("1,5 gb") == 1.5


def test_hardware_memory_parser_rejects_missing_or_unknown_units():
    assert parse_memory_gb(None) is None
    assert parse_memory_gb("16") is None
    assert parse_memory_gb("unknown") is None
