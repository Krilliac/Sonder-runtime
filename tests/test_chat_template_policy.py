from __future__ import annotations

import pytest

from sonder_runtime.domain.chat_template_policy import (
    ChatTemplateOptionsError,
    normalize_chat_template_options,
)


def test_empty_options_do_not_add_template_controls():
    assert normalize_chat_template_options({}) == {}


def test_none_alias_is_canonicalized_to_off():
    assert normalize_chat_template_options({"reasoning_effort": "none"}) == {
        "reasoning_effort": "off"
    }


def test_invalid_effort_fails_closed():
    with pytest.raises(ChatTemplateOptionsError, match="reasoning_effort"):
        normalize_chat_template_options({"reasoning_effort": "turbo"})


def test_invalid_nested_kwargs_fails_closed():
    with pytest.raises(ChatTemplateOptionsError, match="chat_template_kwargs"):
        normalize_chat_template_options({"chat_template_kwargs": "not-an-object"})
