"""Per-operation consent for approved private Sonder Inference workers.

``OperationContext.private_node_allowed`` is tri-state: ``None`` is the legacy
"unspecified" value (workers stay allowed when the operator configured them and
set SONDER_ALLOW_REMOTE_INFERENCE=1), ``False`` is an explicit refusal.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from sonder_runtime.adapters.inference import sonder_responses_client as responses
from sonder_runtime.adapters.inference.sonder_responses_client import (
    SonderResponsesConfig,
    validate_endpoint_policy,
)
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.application.context import local_owner_context, narrow_private_node
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Forbidden
from tests.test_sonder_inference_private_workers import (
    PRIMARY,
    WORKER,
    Hosts,
    _env,
    _gateway,
)


@pytest.fixture
def ca_bundle(tmp_path):
    path = tmp_path / "private-ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
    return str(path)


def _ctx(private_node_allowed):
    return local_owner_context(
        correlation_id="turn-1", source="mcp", cloud_allowed=False,
        private_node_allowed=private_node_allowed, timeout_seconds=30.0,
    )


def _run(gateway, ctx, prompts="abcdef"):
    for prompt in prompts:
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), ctx)


def test_false_sends_nothing_to_the_worker_and_primary_serves(ca_bundle):
    hosts = Hosts()
    _run(_gateway(hosts, _env(ca_bundle)), _ctx(False))
    assert [base for base, _h, _ca in hosts.posts] == [PRIMARY] * 6
    assert all(base != WORKER for base, _h, _ca in hosts.gets)


def test_worker_serves_alone_is_not_used_when_false(ca_bundle):
    # The worker is the only endpoint that is ready; False still refuses it.
    hosts = Hosts()
    hosts.health[PRIMARY] = dict(hosts.health[PRIMARY], status="starting")
    gateway = _gateway(hosts, _env(ca_bundle))
    try:
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(False))
    except Exception:  # noqa: BLE001 - the not-ready primary reports its own refusal
        pass
    assert [base for base, _h, _ca in hosts.posts if base == WORKER] == []


def test_fallback_local_only_context_never_reaches_a_worker(ca_bundle):
    from sonder_runtime.adapters.inference.sonder_inference_gateway import (
        SonderInferenceUnreachable,
    )

    class DownPrimary:
        capabilities = frozenset()

        def generate(self, request, context):
            raise SonderInferenceUnreachable("primary down")

    hosts = Hosts()
    fallback = _gateway(hosts, _env(ca_bundle))
    wrapper = PreSendFallbackGateway(DownPrimary(), fallback=fallback)
    for prompt in "abcdef":
        wrapper.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx(None))
    assert hosts.posts, "the fallback must still serve from the primary endpoint"
    assert all(base != WORKER for base, _h, _ca in hosts.posts)


def test_none_keeps_legacy_placement_on_the_worker(ca_bundle):
    hosts = Hosts()
    _run(_gateway(hosts, _env(ca_bundle)), _ctx(None), prompts="abcd")
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY, WORKER}


def test_true_places_on_the_worker_like_none(ca_bundle):
    hosts = Hosts()
    _run(_gateway(hosts, _env(ca_bundle)), _ctx(True), prompts="abcd")
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY, WORKER}


def test_narrowing_never_widens():
    assert narrow_private_node(True, False) is False
    assert narrow_private_node(None, False) is False
    assert narrow_private_node(False, True) is False
    assert narrow_private_node(False, None) is False
    assert narrow_private_node(None, None) is None
    assert narrow_private_node(True, None) is True
    assert narrow_private_node(None, True) is True
    assert narrow_private_node(True, True) is True


# -- the Responses client's private_worker mode ------------------------------


def _worker_cfg(tmp_path: Path) -> SonderResponsesConfig:
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n", encoding="utf-8")
    return SonderResponsesConfig(
        base_url="https://10.77.0.2:8443", private_worker=True, allow_remote=True,
        api_key="node1-secret-token", ca_bundle=str(ca),
    )


def test_responses_private_worker_refuses_false_and_allows_none_and_true(tmp_path):
    cfg = _worker_cfg(tmp_path)
    with pytest.raises(Forbidden, match="consent"):
        validate_endpoint_policy(cfg, context=_ctx(False))
    validate_endpoint_policy(cfg, context=_ctx(None))
    validate_endpoint_policy(cfg, context=_ctx(True))
    validate_endpoint_policy(cfg, context=None)


def test_responses_private_worker_create_sends_nothing_when_false(tmp_path):
    opened = []

    class _Opener(urllib.request.OpenerDirector):
        def open(self, fullurl, data=None, timeout=None):  # type: ignore[override]
            opened.append(fullurl)
            raise AssertionError("no request may be opened without consent")

    client = responses.SonderResponsesClient(
        _worker_cfg(tmp_path), opener_factory=lambda _budget: _Opener(),
    )
    with pytest.raises(Forbidden, match="consent"):
        client.create(responses.ResponsesRequest(input="ping", priority="subagent"),
                      context=_ctx(False))
    assert opened == []
