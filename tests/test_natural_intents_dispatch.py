"""``natural_intents.dispatch``: run/train intents go only through the slash gate.

``serve._handle_intent`` delegates here, so these pin the contract it relies on:
the intent's actions become ``/run`` and ``/train N`` handed to the surface's
gated slash handler, and a caller without developer authority (or one whose
authority check raises) gets a refusal per line with the handler never called.
"""
from __future__ import annotations

import pytest

from sonder_runtime.application.command_surface import natural_intents

pytestmark = pytest.mark.unit


def _never(_command):
    pytest.fail("slash handler called for a refused caller")


@pytest.mark.parametrize(("intent", "expected"), [
    ({}, []),
    ({"trace": True, "strict": False}, []),
    ({"run": True}, ["/run"]),
    ({"run": False}, []),
    ({"train": 3}, ["/train 3"]),
    ({"train": 0}, ["/train 0"]),
    ({"train": True}, []),
    ({"train": "3"}, []),
    ({"train": 2, "run": True}, ["/run", "/train 2"]),
])
def test_slash_commands_maps_run_then_train(intent, expected):
    assert natural_intents.slash_commands(intent) == expected


def test_authorized_caller_dispatches_each_line_through_the_slash_handler():
    seen = []

    def slash(command):
        seen.append(command)
        return f"ok {command}"

    replies = natural_intents.dispatch({"run": True, "train": 4}, slash, lambda: True)

    assert seen == ["/run", "/train 4"]
    assert replies == ["ok /run", "ok /train 4"]


def test_unauthorized_caller_is_refused_without_dispatch():
    replies = natural_intents.dispatch({"run": True, "train": 4}, _never, lambda: False)

    assert replies == [
        "refused /run: developer or admin authentication is required",
        "refused /train: developer or admin authentication is required",
    ]


@pytest.mark.parametrize("error", [KeyError("account"), TypeError("bad"), AttributeError("get")])
def test_authority_check_that_raises_fails_closed(error):
    def authorized():
        raise error

    replies = natural_intents.dispatch({"run": True}, _never, authorized)

    assert replies == ["refused /run: developer or admin authentication is required"]


def test_authority_is_not_asked_when_there_is_nothing_to_dispatch():
    def authorized():
        pytest.fail("authority checked for an intent with no run/train action")

    assert natural_intents.dispatch({"trace": True}, _never, authorized) == []
