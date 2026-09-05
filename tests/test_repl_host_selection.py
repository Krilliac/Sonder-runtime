from dataclasses import replace
import time
import pytest

from sonder_runtime.adapters import memory_store
from sonder_runtime.application.context import local_owner_context


@pytest.fixture
def setup(tmp_path):
    from sonder_runtime.bootstrap.repl_host_selection import (
        ReplHostSelectionAdapter,
        ReplHostPolicy,
    )

    project = tmp_path / "project"
    project.mkdir()
    conn = memory_store.connect(str(tmp_path / "memory.db"))
    memory_store.init_db(conn)
    context = local_owner_context(
        correlation_id="host", workspace_roots=(project,), timeout_seconds=60
    )
    state = [
        ReplHostPolicy(
            "host-grant", 1, time.time() + 60, (str(project),), ("read_file",)
        )
    ]
    adapter = ReplHostSelectionAdapter(
        get_session=lambda identity: memory_store.get_session(conn, identity),
        find_session=lambda query: memory_store.find_session(conn, query),
        touch_session=lambda identity: memory_store.touch_session(conn, identity),
        policy=lambda context, identity: state[0],
    )
    yield adapter, context, state, conn
    conn.close()


def test_fresh_requires_persisted_row_and_private_scope(setup):
    adapter, context, state, conn = setup
    identity = "a" * 16
    with pytest.raises(PermissionError):
        adapter.select_exact(identity, context)
    selection = adapter.create(identity, context)
    assert memory_store.get_session(conn, identity)["session_id"] == identity
    with pytest.raises(PermissionError):
        adapter.authorize(context, selection.host_conversation_id)
    with adapter.scope(selection, context):
        grant = adapter.authorize(context, selection.host_conversation_id)
        assert grant.host_conversation_id == "repl-session:" + identity
        assert grant.workspace_roots == state[0].workspace_roots


def test_title_prefix_is_resolved_once_to_exact_row(setup):
    adapter, context, state, conn = setup
    adapter.create("a" * 16, context)
    memory_store.set_session_title(conn, "a" * 16, "Example first")
    selection = adapter.select_resolved("Example", context)
    memory_store.touch_session(conn, "b" * 16)
    memory_store.set_session_title(conn, "b" * 16, "Example newer")
    with adapter.scope(selection, context):
        assert adapter.authorize(
            context, selection.host_conversation_id
        ).host_conversation_id.endswith("a" * 16)


@pytest.mark.parametrize(
    "change", ["delete", "revision", "expiry", "clear", "reselect"]
)
def test_live_revocations_fence_existing_scope(setup, change):
    adapter, context, state, conn = setup
    selection = adapter.create("a" * 16, context)
    with adapter.scope(selection, context):
        if change == "delete":
            conn.execute("DELETE FROM sessions")
            conn.commit()
        elif change == "revision":
            state[0] = replace(state[0], revision=2)
        elif change == "expiry":
            state[0] = replace(state[0], expires_at=time.time() - 1)
        elif change == "clear":
            adapter.clear()
        else:
            adapter.select_exact("a" * 16, context)
        with pytest.raises(PermissionError):
            adapter.authorize(context, selection.host_conversation_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "mcp"),
        ("source", "http"),
        ("principal_id", "foreign"),
        ("auth_level", "admin"),
    ],
)
def test_non_local_operator_context_cannot_select_or_authorize(setup, field, value):
    adapter, context, state, conn = setup
    selection = adapter.create("a" * 16, context)
    foreign = replace(context, **{field: value})
    with pytest.raises(PermissionError):
        adapter.select_exact("a" * 16, foreign)
    with adapter.scope(selection, context):
        with pytest.raises(PermissionError):
            adapter.authorize(foreign, selection.host_conversation_id)


def test_context_isolation_and_clear_does_not_resurrect_selection(setup):
    from contextvars import Context

    adapter, context, state, conn = setup
    selection = adapter.create("a" * 16, context)
    with adapter.scope(selection, context):
        with pytest.raises(PermissionError):
            Context().run(adapter.authorize, context, selection.host_conversation_id)
        with pytest.raises(RuntimeError):
            with adapter.scope(selection, context):
                adapter.clear()
                raise RuntimeError("leave nested scope")
        with pytest.raises(PermissionError):
            adapter.authorize(context, selection.host_conversation_id)


def test_foreign_issuer_copy_and_cross_thread_scope_denied(setup):
    from concurrent.futures import ThreadPoolExecutor

    adapter, context, state, conn = setup
    selection = adapter.create("a" * 16, context)
    for foreign in (replace(selection, _issuer=object()), replace(selection)):
        with pytest.raises(PermissionError):
            with adapter.scope(foreign, context):
                pass
    with adapter.scope(selection, context):
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(PermissionError):
                pool.submit(
                    adapter.authorize, context, selection.host_conversation_id
                ).result()
            pool.submit(adapter.clear).result()
        with pytest.raises(PermissionError):
            adapter.authorize(context, selection.host_conversation_id)


def test_live_attenuation_preserves_original_expiry_and_refuses_expansion(setup):
    adapter, context, state, conn = setup
    state[0] = replace(state[0], allowed_tools=("read_file", "write_file"))
    selection = adapter.create("a" * 16, context)
    child = __import__("pathlib").Path(state[0].workspace_roots[0]) / "child"
    child.mkdir()
    with adapter.scope(selection, context):
        state[0] = replace(
            state[0],
            allowed_tools=("read_file",),
            workspace_roots=(str(child),),
            expires_at=state[0].expires_at + 100,
        )
        grant = adapter.authorize(context, selection.host_conversation_id)
        assert grant.expires_at == selection.policy.expires_at
        assert grant.workspace_roots == (str(child),)
        assert grant.allowed_tools == ("read_file",)
        state[0] = replace(state[0], allowed_tools=("execute",))
        with pytest.raises(PermissionError):
            adapter.authorize(context, selection.host_conversation_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", True),
        ("revision", 0),
        ("expires_at", float("nan")),
        ("allowed_tools", ["read_file"]),
        ("allowed_tools", ("read_file", "read_file")),
        ("workspace_roots", ("relative",)),
        ("grant_id", ""),
    ],
)
def test_invalid_host_policy_shapes_rejected(setup, field, value):
    adapter, context, state, conn = setup
    with pytest.raises(ValueError):
        replace(state[0], **{field: value})


def test_exact_resolver_wrong_row_and_missing_creation_are_rejected(setup):
    adapter, context, state, conn = setup
    adapter._touch_session = lambda identity: None
    with pytest.raises(PermissionError):
        adapter.create("a" * 16, context)
    adapter._get_session = lambda identity: {"session_id": "b" * 16}
    with pytest.raises(PermissionError):
        adapter.select_exact("a" * 16, context)


@pytest.mark.parametrize(
    "change", ["cancellation", "deadline", "cloud", "remote", "roots"]
)
def test_scope_context_cannot_be_replaced_with_broader_authority(setup, change):
    adapter, context, state, conn = setup
    selection = adapter.create("a" * 16, context)
    changes = {
        "cancellation": {
            "cancellation": local_owner_context(correlation_id="new").cancellation
        },
        "deadline": {"deadline_monotonic": context.deadline_monotonic + 10},
        "cloud": {"cloud_allowed": True},
        "remote": {"remote_ollama_allowed": True},
        "roots": {"workspace_roots": (context.workspace_roots[0].parent,)},
    }
    with adapter.scope(selection, context):
        with pytest.raises(PermissionError):
            adapter.authorize(
                replace(context, **changes[change]), selection.host_conversation_id
            )


def test_scope_accepts_attenuation_but_original_cancellation_remains_live(setup):
    adapter, context, state, conn = setup

    class Token:
        cancelled = False

    token = Token()
    context = replace(context, cancellation=token)
    selection = adapter.create("a" * 16, context)
    with adapter.scope(selection, context):
        narrowed = replace(context, deadline_monotonic=context.deadline_monotonic - 1)
        assert adapter.authorize(narrowed, selection.host_conversation_id)
        token.cancelled = True
        with pytest.raises(PermissionError):
            adapter.authorize(narrowed, selection.host_conversation_id)
