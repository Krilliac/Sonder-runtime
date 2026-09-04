from dataclasses import replace
import pytest

from tests.test_compute_placement_service import _service, _request, _envelope


def test_rank_all_can_choose_local_without_remote_failure():
    from sonder_runtime.domain.compute_fabric import PlacementPolicy
    service, transport, local_worker = _service()
    request = replace(_request(), placement_policy=PlacementPolicy.RANK_ALL)
    # Identical resources tie by node identity: 'linux-node' sorts before
    # 'local', so explicitly prefer local to prove it participates in ranking.
    request = replace(request, preferred_node_ids=frozenset({'local'}))
    result = service.submit(request, _envelope())
    assert result.node_id == 'local'
    assert local_worker.calls == 1 and transport.submit_calls == 0


def test_rank_all_never_grants_remote_consent():
    from sonder_runtime.domain.compute_fabric import PlacementPolicy
    service, transport, local_worker = _service()
    request = replace(_request(allow_remote=False), placement_policy=PlacementPolicy.RANK_ALL,
                      preferred_node_ids=frozenset({'linux-node'}))
    assert service.submit(request, _envelope()).node_id == 'local'
    assert local_worker.calls == 1 and transport.submit_calls == 0


def test_explicit_local_policy_narrows_remote_consent():
    from sonder_runtime.domain.compute_fabric import PlacementPolicy
    service, transport, local_worker = _service()
    request = replace(_request(), placement_policy=PlacementPolicy.LOCAL_ONLY)
    assert service.submit(request, _envelope()).node_id == 'local'
    assert transport.submit_calls == 0


def test_policy_is_digest_bound_without_changing_legacy_request_shape():
    from sonder_runtime.domain.compute_fabric import PlacementPolicy
    request = _request()
    assert 'placement_policy' not in request.as_dict()
    ranked = replace(request, placement_policy=PlacementPolicy.RANK_ALL)
    assert ranked.as_dict()['placement_policy'] == 'rank-all'
    assert request.digest() != ranked.digest()


def test_direct_scheduler_enforces_local_policy():
    from tests.test_compute_placement_service import _node, _snapshot, NOW
    from sonder_runtime.domain.compute_fabric import PlacementPolicy, ComputePlacementScheduler
    remote = _snapshot(_node('remote', local=False))
    request = replace(_request(), placement_policy=PlacementPolicy.LOCAL_ONLY)
    result = ComputePlacementScheduler().place(request, (remote,), now=NOW)
    assert result.selected_node_id is None
    assert result.candidates[0].reason_code == 'local_only'


def test_reusing_request_id_cannot_change_placement_policy():
    from sonder_runtime.domain.compute_fabric import PlacementPolicy
    from sonder_runtime.domain.common.errors import Conflict
    service, transport, local_worker = _service()
    service.submit(_request(), _envelope())
    with pytest.raises(Conflict, match='placement'):
        service.submit(replace(_request(), placement_policy=PlacementPolicy.LOCAL_ONLY), _envelope())
    assert transport.submit_calls == 1 and local_worker.calls == 0
