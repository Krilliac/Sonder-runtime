from sonder_runtime.application.session.checkpoints import checkpoint_projection
from sonder_runtime.application.session.fork import fork_session
from sonder_runtime.domain.common.events import DomainEvent


def _events():
    return tuple(DomainEvent("message.user", "session", "s1", n, {"content": str(n)}) for n in (1, 2, 3))


def test_fork_requires_explicit_existing_boundary_and_preserves_lineage():
    lineage, prefix = fork_session("s1", _events(), fork_sequence=2, child_session_id="s2")
    assert lineage.parent_session_id == "s1"
    assert lineage.fork_sequence == 2
    assert [event.sequence for event in prefix] == [1, 2]


def test_checkpoint_is_bound_to_source_sequence_and_hash():
    checkpoint = checkpoint_projection(_events(), source_hash="hash-3")
    assert not checkpoint.is_stale(3, "hash-3")
    assert checkpoint.is_stale(4, "hash-4")
