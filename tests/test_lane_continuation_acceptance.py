"""Scripted model, actual typed edit/test jobs, and explicit host approval resume."""

from dataclasses import replace
import time

from tests.test_lane_coding_acceptance import coding, make_service, tool
from tests.test_lane_continuation import granted
from tests.test_lane_pending_verification import pending
from tests.test_lane_continuation_projection import Codec, HostProjection


def test_real_catalog_certificate_after_explicit_pending_host_reattachment(coding):
    from sonder_runtime.application.agents.lane_continuation import (
        LaneContinuationService,
    )
    from sonder_runtime.application.ports.lane_continuation import (
        HostContinuationGrant,
        ProjectionBinding,
    )
    from sonder_runtime.bootstrap.delegated_verification import (
        compose_delegated_verification,
    )

    repo, catalog, store, sessions, jobs, provider, facade, original_context = coding
    service, model = make_service(
        coding,
        [
            tool("run_tests", target="unit"),
            tool(
                "edit_file",
                path="calc.py",
                old="return sum(values) + 1",
                new="return sum(values)",
            ),
            tool("run_tests", target="unit"),
            "Completed the repair and regression tests.",
        ],
    )
    context = replace(original_context, deadline_monotonic=time.monotonic() + 120)
    parent = service.open_model_parent(context)
    child = service.spawn(
        command_id="child",
        parent_session_id=parent["parent_session_id"],
        task="Repair total",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    service.run_pending(child, context)
    assert store.read_lane(child)["status"] == "completed"
    original_model_calls = len(model.requests)
    grant = HostContinuationGrant(
        context.principal_id,
        "verified-host-task",
        "host-grant",
        1,
        time.time() + 300,
        (str(repo),),
        tuple(sorted(service.allowed_tools)),
    )
    codec = Codec()
    host = LaneContinuationService(
        service,
        authorize_host=lambda ctx, name: grant,
        projection_codec=codec,
        model_writable_roots=lambda: (repo,),
    )
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "verified-host-task",
        context=context,
        command_id="register",
    )
    verifier = compose_delegated_verification(
        service, provider, catalog, targets={str(repo): "unit"}
    )
    prepared = bound.prepare_verification(verifier, command_id="verify")
    binding = ProjectionBinding(
        bound.continuation_id,
        context.principal_id,
        "run",
        "verified-host-task",
        parent["parent_session_id"],
        1,
        prepared.verification_id,
        prepared.bundle_digest,
        prepared.roots,
        1,
    )
    identity = bound.link_pending_verification(
        verifier,
        prepared,
        HostProjection(binding, True, "VALIDATION_FAILED", codec.issuer),
    )
    count_before = len(jobs.list(include_terminal=True, limit=20))
    assert (
        bound.execute_verification(verifier, prepared, approve=pending)["state"]
        == "approval_pending"
    )
    assert len(jobs.list(include_terminal=True, limit=20)) == count_before
    bound.close()
    fresh_context = replace(context, correlation_id="fresh-host")
    restored_host = LaneContinuationService(
        service,
        authorize_host=lambda ctx, name: grant,
        projection_codec=codec,
        model_writable_roots=lambda: (repo,),
    )
    selected = restored_host.select(identity.continuation_id, fresh_context)
    attachment = restored_host.prepare_reattachment(
        selected, fresh_context, command_id="reattach"
    )
    fresh = restored_host.execute_reattachment(
        attachment, fresh_context, approve=granted
    )
    result = verifier.resume_pending_approval(fresh, identity, approve=granted)
    assert result["state"] == "certified", result
    verdict = fresh.verification_view(
        verifier, prepared.verification_id, action="validate"
    )
    assert verdict.valid is True
    assert len(jobs.list(include_terminal=True, limit=20)) == count_before + 1
    assert len(model.requests) == original_model_calls
    assert fresh.terminal_projection(identity).terminal_class == "VALIDATION_FAILED"
    for proof in result["certificate"]["cleanup_proofs"]:
        assert (
            proof["process_exited"]
            and proof["containment_empty"]
            and proof["resources_released"]
        )
    fresh.close()
    service.close()
