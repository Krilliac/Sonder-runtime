from dataclasses import replace
import pytest
from sonder_runtime.application.ports.terminal_output import TerminalOutputReference


def test_reference_binds_full_digests_and_exact_utf8_size():
    reference = TerminalOutputReference("a" * 64, 65536, "b" * 64)
    assert reference.size_bytes == 65536
    assert reference.binding_sha256 != reference.sha256


@pytest.mark.parametrize(
    "change",
    [
        dict(size_bytes=True),
        dict(size_bytes=-1),
        dict(size_bytes=1048577),
        dict(sha256="A" * 64),
        dict(binding_sha256="b" * 63),
    ],
)
def test_reference_rejects_ambiguous_or_unbounded_values(change):
    with pytest.raises(ValueError):
        replace(TerminalOutputReference("a" * 64, 65536, "b" * 64), **change)
