"""The client executor's git driver overrides accept the same names as the server guard.

``_git_driver_overrides`` builds ``-c <section>.<name>.<key>=<value>`` for each
configured filter, merge and diff driver. A name ``-c`` cannot express exactly
(git splits at the first '='; a quote, space, slash or non-ASCII character
may not round-trip) must fail closed, so the executor refuses to run git. The
allowed charset is ``[A-Za-z0-9_.-]``, the same as
``sonder_runtime.adapters.git_program_guard``.
"""
from __future__ import annotations

import pytest

from sonder_runtime.adapters import git_program_guard
from sonder_runtime.interfaces.client_agent import executor

_UNSAFE_KEYS = [
    "filter.a=b.clean",
    "filter.a=b.process",
    "merge.m=x.driver",
    "diff.d=e.textconv",
    "filter.a b.clean",
    "filter.a\tb.clean",
    "filter.a\"b.clean",
    "filter.a/b.clean",
    "filter.a+b.clean",
    "filter.a~b.clean",
    "filter.é.clean",
    "filter.a�b.clean",
    "filter.K.clean",
    "filter.ſ.clean",
]
_SAFE_KEYS = ["filter.lfs.clean", "filter.my-filter_2.smudge", "filter.a.b.process", "merge.Ours.driver"]


@pytest.mark.parametrize("key", _UNSAFE_KEYS)
def test_unsafe_driver_name_is_refused(key: str):
    with pytest.raises(ValueError, match="refusing to run git"):
        executor._git_driver_overrides(key + "\x00")


@pytest.mark.parametrize("key", _SAFE_KEYS)
def test_safe_driver_name_is_overridden(key: str):
    overrides = executor._git_driver_overrides(key + "\x00")
    section, _, rest = key.partition(".")
    name = rest.rsplit(".", 1)[0]
    assert overrides
    assert all(arg.startswith(f"{section.lower()}.{name}.") for arg in overrides[1::2])


@pytest.mark.parametrize("key", [*_UNSAFE_KEYS, *_SAFE_KEYS])
def test_client_and_server_guards_agree_on_driver_names(key: str):
    try:
        executor._git_driver_overrides(key + "\x00")
    except ValueError:
        client_refuses = True
    else:
        client_refuses = False
    try:
        git_program_guard.driver_overrides([key])
    except git_program_guard.GitProgramConfigError:
        server_refuses = True
    else:
        server_refuses = False
    assert client_refuses == server_refuses
