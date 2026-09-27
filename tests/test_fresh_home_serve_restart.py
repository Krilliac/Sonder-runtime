"""A brand-new state home must serve, stop, and serve again.

Regression: ``serve`` on a fresh home created ``memory.db`` and the other
stores without the SPEC-5 epoch marker, so the next ``serve`` on the same
home refused with "State is not fully adopted at the SPEC-5 schema epoch".
Two things combined:

* the epoch gate treated "no ``memory.db``" as a fresh install that "will
  create epoch 2 directly", but nothing on the serve path ever stamped it;
* the startup preflight's schema check opened every store read-write, which
  created ``memory.db`` (without a marker) before the gate ran, so even the
  first ``serve`` of a fresh home was refused.

A pre-epoch home (legacy state present) must still be refused and pointed at
the explicit ``migrate --adopt-epoch2`` (see test_typed_home_entrypoint).
"""
from __future__ import annotations

import argparse

import pytest

from sonder_runtime.__main__ import _load_config, _run_preflight, main
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH2_DATABASES,
    check_epoch,
    require_epoch_2,
)


class _ReachedListener(Exception):
    """Raised where serve would compose the app and bind the listener."""


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "fresh-home"
    home.mkdir()
    for name in ("SONDER_HOME", "SONDER_CONFIG", "SONDER_SECRETS", "SONDER_DB",
                 "SONDER_FLEET_DB", "SONDER_FLEET_PRINCIPAL_FILE", "OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONDER_HOME", str(home))

    def stop(*_args, **_kwargs):
        raise _ReachedListener()

    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", stop)
    return home


def _serve(home, *flags):
    with pytest.raises(_ReachedListener):
        main(["serve", "--skip-ollama", *flags, "--set", f"state.home={home}", "11447"])


# With the real preflight it must not create stores behind the gate's back;
# without it, the fresh-install stamp alone must carry the second start.
@pytest.mark.parametrize("flags", [(), ("--skip-preflight",)], ids=["preflight", "no-preflight"])
def test_fresh_home_serves_twice(fresh_home, capsys, flags):
    _serve(fresh_home, *flags)
    assert "migration required" not in capsys.readouterr().err
    for name in EPOCH2_DATABASES:
        assert check_epoch(fresh_home / name) == 2, name
    require_epoch_2(fresh_home)

    _serve(fresh_home, *flags)
    assert "migration required" not in capsys.readouterr().err


def test_preflight_does_not_create_stores_on_a_fresh_home(fresh_home):
    args = argparse.Namespace(config=None, secrets=None,
                              set=[f"state.home={fresh_home}"])
    _run_preflight(_load_config(args), check_ollama=False)
    assert not (fresh_home / "memory.db").exists()
    assert not any(path.suffix == ".db" for path in fresh_home.iterdir())
