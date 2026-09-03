"""Boundary tests for WP1 callable_inspection migration."""
import server
from sonder_runtime.domain import callable_inspection


def test_root_helper_is_identity_preserving_alias():
    assert server._callable_accepts_keyword is callable_inspection.callable_accepts_keyword


def test_accepts_declared_keyword():
    def fn(*, name="default"):
        pass
    assert callable_inspection.callable_accepts_keyword(fn, "name") is True


def test_rejects_undeclared_keyword():
    def fn(*, other="x"):
        pass
    assert callable_inspection.callable_accepts_keyword(fn, "name") is False


def test_accepts_var_keyword():
    def fn(**kwargs):
        pass
    assert callable_inspection.callable_accepts_keyword(fn, "anything") is True


def test_unsignable_returns_true():
    assert callable_inspection.callable_accepts_keyword(print, "end") is True
