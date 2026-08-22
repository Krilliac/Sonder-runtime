from __future__ import annotations

import sys

from sonder_runtime.adapters.web_intents import web_intents
from sonder_runtime.adapters.repl_services import web_intents as repl_web_intents


def test_repl_uses_packaged_web_intent_provider_without_eager_legacy_import():
    assert repl_web_intents is web_intents
    assert "web_intents" not in sys.modules or sys.modules["web_intents"].__name__ == "web_intents"


def test_packaged_provider_preserves_classifier_contract():
    assert web_intents.classify("How do I use the web?") == {"kind": "capability"}
