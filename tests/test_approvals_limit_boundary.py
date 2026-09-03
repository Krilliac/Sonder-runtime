"""Boundary tests for WP1 approvals_limit migration."""
import server
from sonder_runtime.domain import approvals_limit


def test_root_helper_is_identity_preserving_alias():
    assert server._approvals_limit is approvals_limit.approvals_limit


def test_empty_returns_default():
    assert approvals_limit.approvals_limit("") == 20
    assert approvals_limit.approvals_limit(None) == 20


def test_valid_number_is_clamped():
    assert approvals_limit.approvals_limit("5") == 5
    assert approvals_limit.approvals_limit("0") == 1
    assert approvals_limit.approvals_limit("-1") == 1
    assert approvals_limit.approvals_limit("300") == 200


def test_non_numeric_returns_default():
    assert approvals_limit.approvals_limit("abc") == 20
    assert approvals_limit.approvals_limit("ten") == 20
