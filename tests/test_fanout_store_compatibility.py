"""Ownership and identity contracts for the packaged fanout store adapter."""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_root_fanout_store_is_the_canonical_module_object():
    legacy = importlib.import_module("fanout_store")
    packaged = importlib.import_module(
        "sonder_runtime.adapters.persistence.fanout_store"
    )

    assert legacy is packaged
    assert sys.modules["fanout_store"] is packaged
    assert Path(packaged.__file__).resolve() == (
        Path(__file__).resolve().parents[1]
        / "sonder_runtime"
        / "adapters"
        / "persistence"
        / "fanout_store.py"
    ).resolve()


def test_packaged_fanout_store_owns_private_transaction_and_schema_seams():
    store = importlib.import_module(
        "sonder_runtime.adapters.persistence.fanout_store"
    )

    for name in (
        "_SCHEMA",
        "_connect",
        "_write_transaction",
        "_ensure_schema",
        "reset_schema_cache_for_tests",
    ):
        assert hasattr(store, name)
    assert store._connect.__module__ == store.__name__
    assert store._write_transaction.__module__ == store.__name__


def test_server_uses_packaged_fanout_store_directly():
    import server

    packaged = importlib.import_module(
        "sonder_runtime.adapters.persistence.fanout_store"
    )
    assert server.fanout_store is packaged


def test_root_fanout_store_contains_no_implementation_functions():
    root = Path(__file__).resolve().parents[1] / "fanout_store.py"
    tree = ast.parse(root.read_text(encoding="utf-8"))
    assert not [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
