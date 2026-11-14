import pytest

from sonder_runtime.platform import declared_context as dc


def test_declared_value_is_parsed():
    assert dc.declared_num_ctx("num_ctx                        8192") == 8192


def test_absent_parameter_yields_none():
    assert dc.declared_num_ctx("temperature   0.7\nstop   <end>") is None
    assert dc.declared_num_ctx("") is None
    assert dc.declared_num_ctx(None) is None
    assert dc.declared_num_ctx(8192) is None


def test_num_ctx_among_several_parameters():
    text = "temperature                    0.6\nnum_ctx                        4096\ntop_k                          20\n"
    assert dc.declared_num_ctx(text) == 4096


def test_similarly_named_parameter_is_not_num_ctx():
    assert dc.declared_num_ctx("num_ctx_extra   100\nnum_predict   200") is None


def test_last_declaration_wins():
    assert dc.declared_num_ctx("num_ctx 2048\nnum_ctx 4096") == 4096


@pytest.mark.parametrize("text", [
    "num_ctx", "num_ctx abc", "num_ctx 12.5", "num_ctx 8k", "num_ctx 0", "num_ctx -4096", "num_ctx 8192 9000",
])
def test_malformed_or_non_positive_is_none(text):
    assert dc.declared_num_ctx(text) is None


def test_resolve_clamps_only_by_advertised_length():
    assert dc.resolve_declared_context(8192, 262144) == 8192
    assert dc.resolve_declared_context(8192, None) == 8192
    assert dc.resolve_declared_context(65536, 4096) == 4096


def test_resolve_without_declaration_is_none():
    assert dc.resolve_declared_context(None, 262144) is None
    assert dc.resolve_declared_context(0, 262144) is None


def test_operator_pin_detection(monkeypatch):
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)
    assert dc.operator_pinned() is False
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "16k")
    assert dc.operator_pinned() is True
