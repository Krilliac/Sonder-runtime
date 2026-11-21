"""The real-kit meshgen tests take the kit path from a harness variable, never from a hard-coded private path."""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).parent
_KIT_TEST_FILES = ("test_meshgen_integration.py", "test_meshgen_ported_scripts.py")


def test_the_meshgen_kit_variable_survives_the_ambient_scrub():
    conftest = sys.modules["conftest"]
    assert "SONDER_TEST_MESHGEN_KIT" in conftest._HARNESS_VARIABLES


def test_the_kit_tests_read_the_kit_from_the_variable_and_name_no_private_path():
    for name in _KIT_TEST_FILES:
        text = (_TESTS / name).read_text(encoding="utf-8")
        assert "SONDER_TEST_MESHGEN_KIT" in text, name
        assert "lanternfall" not in text.lower(), name
        assert "D:\\" not in text, name
