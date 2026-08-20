import server
from sonder_runtime.domain.interaction_footer import (
    DEFAULT_FOOTER_PREFIX,
    trailing_interaction_id,
)


def test_server_keeps_identity_compatible_footer_parser():
    assert server._trailing_interaction_id is trailing_interaction_id


def test_parser_returns_opaque_id_from_footer():
    assert trailing_interaction_id(
        "answer\n\n[interaction_id: id-with-new-format]"
    ) == "id-with-new-format"


def test_parser_uses_last_footer_and_rejects_malformed_values():
    assert trailing_interaction_id(
        "old\n\n[interaction_id: old]\nnew\n\n[interaction_id: fresh]"
    ) == "fresh"
    assert trailing_interaction_id("answer") is None
    assert trailing_interaction_id("answer\n\n[interaction_id: ]") is None
    assert trailing_interaction_id("answer\n\n[interaction_id: id") is None


def test_parser_accepts_an_explicit_footer_delimiter():
    assert trailing_interaction_id(
        "answer\n<receipt: custom]", "\n<receipt: "
    ) == "custom"
    assert DEFAULT_FOOTER_PREFIX == "\n\n[interaction_id: "
