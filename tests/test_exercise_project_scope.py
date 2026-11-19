"""Exercise lane project-scope: repository roots, fleet cues, lane consent.

Each test pins one finding from the ag-integrate exercise triage (MA-07,
MA-11, MA-03, MA-02, MA-17, MA-16, MA-08, MA-15). They run on the
fixture's own temporary directories and fake collaborators; nothing here
starts a model, a fleet or a lane against real state.

Ported from the ag-integrate lane onto main. MA-09 (the parentless child
project check in the depth-1 delegation resolver) and the MA-08 dotted
sub-task list split are not here: main has neither the delegation resolver
nor ``fleet_briefing.extract_subtasks``.
"""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import intents
import master_orchestrator as mo
from sonder_runtime.application.errors import CapacityExceeded, Forbidden, InvalidInput
from sonder_runtime.domain import fleet_briefing
from sonder_runtime.interfaces.http import serve
from tests.test_agent_lane_http_wiring import auth as _lane_auth, lane_http as lane_http


def _lane_request(base, path="/v1/agent-lanes", payload=None):
    """Like the shared helper but with a timeout that survives a loaded box."""
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        base + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture
def isolated_roots(monkeypatch, tmp_path):
    """No ambient file roots: only the checkout and the test state home."""
    monkeypatch.setenv("SONDER_FILE_ROOTS", "")
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(tmp_path / "no-such-roots.local"))
    return tmp_path


def _repo_dir(tmp_path, name="pkg"):
    path = tmp_path / name
    path.mkdir()
    (path / "auth.py").write_text("x = 1\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- MA-07 (a)

def test_directory_in_review_prompt_requires_repository_tools(tmp_path):
    # Decision D1 option B: the role-blind resolver never probes a bare
    # directory named in prose; a label line or a selected project names it.
    repo = _repo_dir(tmp_path)
    prompt = "Review the modules in %s for defects" % repo
    assert mo.requires_repository_tools(prompt) is True
    assert mo.repository_project_root(prompt) == ""
    labelled = "Repository: %s\nReview the modules for defects" % repo
    assert mo.resolve_repository_project_root(labelled) == str(repo.resolve())
    assert mo.resolve_repository_project_root(prompt, project=str(repo)) == str(repo.resolve())


@pytest.mark.parametrize("prompt", [
    "Fix the 1 / 2 ratio in my cake recipe",
    "Read this poem about life / death and summarize",
    "Edit the caption: before / after",
    "Invent a game where you read runes / glyphs",
    "Review my essay and/or outline",
    "Review the plan at https://example.com/x",
])
def test_lone_slash_is_not_a_repository_path(prompt):
    assert mo.requires_repository_tools(prompt) is False


@pytest.mark.parametrize("prompt", [
    "Review D:/x/trialkit for defects",
    "review /srv/app please",
    "Review C:\\work\\pkg now",
    "Spawn three agents to review D:\\x\\trialkit",
])
def test_absolute_path_after_review_verb_is_a_repository_path(prompt):
    assert mo.requires_repository_tools(prompt) is True


def test_missing_directory_is_not_repository_state(tmp_path):
    prompt = "Review the modules in %s" % (tmp_path / "does-not-exist")
    assert mo.repository_project_root(prompt) == ""
    assert mo.requires_repository_tools(prompt) is True  # the noun still asks for source
    with pytest.raises(ValueError, match="Repository: <absolute path>"):
        mo.resolve_repository_project_root(prompt)


def test_unlabelled_directory_is_refused_identically_whether_or_not_it_exists(
    monkeypatch, isolated_roots,
):
    import server
    monkeypatch.setattr(
        server, "_orchestrator_agent_worker",
        lambda *a, **kw: pytest.fail("a refused task started a fleet worker"),
    )
    existing = _repo_dir(isolated_roots, "outside")
    replies = []
    for folder in (existing, isolated_roots / "absent"):
        reply = server.master_orchestrate(
            task="Review the modules in %s" % folder, mode="fleet", agents=2,
        )
        assert "EVIDENCE_REQUIRED" in reply
        assert "Repository: <absolute path>" in reply
        replies.append(reply.replace(str(folder), "<dir>"))
    assert replies[0] == replies[1]


# ---------------------------------------------------------------- MA-11 (b)

def test_work_slash_runs_in_workspace_project(monkeypatch, tmp_path):
    import server
    repo = _repo_dir(tmp_path)
    monkeypatch.setattr(serve, "_http_slash_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(serve, "_account_task_boundary_refusal", lambda *a, **kw: "")
    seen = []
    monkeypatch.setattr(server, "workbench_agent", lambda **kw: seen.append(kw) or "done")
    monkeypatch.setattr(server, "served_work_project", lambda value: str(value) if value else "")
    reply = serve._handle_slash(
        "/work fix it", project="storage-namespace", workspace_project=str(repo),
    )
    assert reply == "done"
    assert [call["project"] for call in seen] == [str(repo)]


_OWNER = {"authorized": True, "mode": "local-open"}
_ACCOUNT = {"authorized": True, "mode": "accounts", "account": {"username": "reader", "role": "user"}}


def _granting_app(monkeypatch, roots):
    app = _fake_app(roots, False)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: app)
    return app


def test_work_project_for_request_uses_directory_named_in_prompt(monkeypatch, isolated_roots):
    repo = _repo_dir(isolated_roots)
    _granting_app(monkeypatch, [repo])
    prompt = "read the files in %s" % repo
    assert serve._work_project_for_request("", "storage", prompt, _OWNER) == str(repo.resolve())
    # A selected project is never replaced by a path in the prose.
    other = _repo_dir(isolated_roots, "other")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(repo) + os.pathsep + str(other))
    assert serve._work_project_for_request(str(other), "storage", prompt, _OWNER) == str(other.resolve())


def test_non_admin_prompt_never_names_a_work_project(monkeypatch, isolated_roots):
    import server
    repo = _repo_dir(isolated_roots)
    _granting_app(monkeypatch, [repo])
    monkeypatch.setattr(
        server.file_ops, "named_directory",
        lambda *a, **kw: pytest.fail("a non-admin prompt reached named_directory"),
    )
    prompt = "read the files in %s" % repo
    assert serve._work_project_for_request("", "storage", prompt, _ACCOUNT) == "default"
    # No auth (an internal caller) never consults the prompt either.
    assert serve._work_project_for_request("", "storage", prompt) == "default"


@pytest.mark.parametrize("spelling", ["\\\\host\\share\\x", "//host/share/x"])
def test_named_directory_never_probes_a_unc_spelling(monkeypatch, tmp_path, spelling):
    from sonder_runtime.adapters.filesystem import file_ops
    monkeypatch.setattr(os.path, "realpath", lambda *a, **kw: pytest.fail("probed %r" % (a,)))
    monkeypatch.setattr(os.path, "isdir", lambda *a, **kw: pytest.fail("probed %r" % (a,)))
    assert file_ops.named_directory("read the files in %s now" % spelling, (str(tmp_path),)) == ""


def test_named_directory_without_roots_returns_nothing(monkeypatch, tmp_path):
    from sonder_runtime.adapters.filesystem import file_ops
    repo = _repo_dir(tmp_path)
    monkeypatch.setattr(os.path, "realpath", lambda *a, **kw: pytest.fail("probed %r" % (a,)))
    assert file_ops.named_directory("read the files in %s" % repo, ()) == ""


def test_prompt_naming_the_state_home_is_not_selected(monkeypatch, isolated_roots):
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.platform import paths
    home = Path(paths.default_home())
    home.mkdir(parents=True, exist_ok=True)
    granted = _repo_dir(isolated_roots, "granted")
    _granting_app(monkeypatch, [granted])
    # allowed_roots() admits the state home, so the old containment check
    # would have selected it; the lane-grant roots the caller passes do not.
    assert file_ops.inside_allowed_roots(home)
    prompt = "read the files in %s" % home
    assert file_ops.named_directory(prompt, serve._http_workspace_roots(_OWNER)) == ""
    chosen = serve._work_project_for_request("", "storage", prompt, _OWNER)
    assert os.path.normcase(chosen) != os.path.normcase(str(home.resolve()))


# ---------------------------------------------------------------- MA-03 (c)

def test_rejected_absolute_project_is_refused_not_defaulted(monkeypatch, isolated_roots):
    import server
    outside = _repo_dir(isolated_roots, "outside")
    monkeypatch.setattr(serve, "_http_slash_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(serve, "_account_task_boundary_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(
        server, "workbench_agent", lambda **kw: pytest.fail("ran in a rejected project"),
    )
    reply = serve._handle_slash(
        "/work fix it", project="storage-namespace", workspace_project=str(outside),
    )
    assert reply.startswith("Refused:")
    assert str(outside) in reply
    assert "file_roots.local" in reply and "workspace_roots" in reply


def test_rejected_project_refusal_decides_unc_and_bare_names_on_text():
    from sonder_runtime.application.agents.delegation import rejected_project_refusal
    calls = []

    def served(value):
        calls.append(value)
        return ""

    for spelling in ("\\\\host\\share\\x", "//host/share/x"):
        reply = rejected_project_refusal(spelling, served)
        assert reply.startswith("Refused:") and "file_roots.local" in reply
    assert calls == []  # a UNC value never reaches the filesystem-backed check
    assert rejected_project_refusal("a-bare-memory-namespace", served) == ""
    assert rejected_project_refusal("default", served) == ""
    assert calls == []


def test_delegation_service_names_the_setting_identically_for_every_path(tmp_path):
    from sonder_runtime.application.agents.delegation import DelegationService
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    service = DelegationService(SimpleNamespace(spawn=lambda **kw: pytest.fail("spawned")))
    context = SimpleNamespace(
        expired=False, cancellation=SimpleNamespace(cancelled=False),
        workspace_roots=(allowed,), session_id="s",
    )
    messages = set()
    for folder in (outside, tmp_path / "absent"):
        with pytest.raises(PermissionError) as raised:
            service.delegate("task", context=context, project=str(folder), parent_session_id="p")
        messages.add(str(raised.value))
    assert len(messages) == 1  # present and absent paths are indistinguishable
    assert "file_roots.local" in messages.pop()


def test_lane_grant_roots_unifies_configured_file_roots_and_default_workspace(
    monkeypatch, tmp_path,
):
    from sonder_runtime.adapters.filesystem import file_ops
    configured = tmp_path / "configured"
    listed = tmp_path / "listed"
    workspaces = tmp_path / "workspaces"
    for folder in (configured, listed, workspaces):
        folder.mkdir()
    roots_file = tmp_path / "file_roots.local"
    roots_file.write_text("# comment\n%s\n" % listed, encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", "")
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(roots_file))
    monkeypatch.setenv("SONDER_DEFAULT_WORKSPACE_ROOT", str(workspaces))
    roots = file_ops.lane_grant_roots((str(configured),))
    assert {Path(r) for r in roots} == {
        configured.resolve(), listed.resolve(), workspaces.resolve(),
    }
    from sonder_runtime.platform import paths
    assert str(Path(paths.default_home()).resolve()) not in roots
    assert str(file_ops.workspace_root()) not in roots


# ---------------------------------------------------------------- MA-02 (d)

def _fake_app(roots, allow_remote):
    return SimpleNamespace(config=SimpleNamespace(
        state=SimpleNamespace(workspace_roots=tuple(roots), home=""),
        ollama=SimpleNamespace(allow_remote=allow_remote),
    ))


@pytest.mark.parametrize("allow_remote", [True, False])
def test_http_admin_context_inherits_configured_remote_consent(
    monkeypatch, tmp_path, allow_remote,
):
    app = _fake_app([tmp_path], allow_remote)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: app)
    context = serve._http_debug_context({"authorized": True, "mode": "local-open"}, "t")
    assert context.remote_ollama_allowed is allow_remote


def test_http_non_admin_context_never_inherits_remote_consent(monkeypatch, tmp_path):
    app = _fake_app([tmp_path], True)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: app)
    account = {"username": "reader", "role": "user"}
    auth = {"authorized": True, "mode": "accounts", "account": account}
    assert serve._http_workspace_roots(auth) == ()
    context = serve._http_debug_context(auth, "t")
    assert context.remote_ollama_allowed is False
    assert tuple(context.workspace_roots) == ()


def test_agent_lane_context_carries_remote_consent_and_unified_roots(monkeypatch, tmp_path):
    import server
    listed = tmp_path / "listed"
    listed.mkdir()
    roots_file = tmp_path / "file_roots.local"
    roots_file.write_text("%s\n" % listed, encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", "")
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(roots_file))
    app = _fake_app([tmp_path / "configured"], True)
    monkeypatch.setattr(server, "_application", lambda: app)
    _application, context = server._agent_lane_context()
    assert context.remote_ollama_allowed is True
    assert Path(listed).resolve() in {Path(r).resolve() for r in context.workspace_roots}


_REMOTE_OLLAMA = "http://192.0.2.10:11434"  # TEST-NET-1: the gate refuses or passes; never contacted


def _consent_app(monkeypatch, tmp_path, consent, spawn):
    """The real delegate route's app, configured by ``load_config`` with *consent*."""
    import permission_modes
    from sonder_runtime.platform.config import load_config
    # The root conftest strips both variables; set them here so the test owns
    # them. A remote worker pool would make even a loopback route remote.
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", consent)
    monkeypatch.delenv("SONDER_OLLAMA_WORKERS", raising=False)
    config = load_config(env={
        "SONDER_ALLOW_REMOTE_OLLAMA": consent, "SONDER_HOME": str(tmp_path / "state"),
    })
    app = SimpleNamespace(config=config, agent_lanes=lambda: SimpleNamespace(spawn=spawn))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: app)
    monkeypatch.setattr(permission_modes, "_LOADED", True)
    monkeypatch.setitem(permission_modes._STATE, "mode", permission_modes.AUTO)
    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda name: None)
    return config


@pytest.mark.parametrize("consent", ["1", "0"])
def test_http_delegate_lane_inherits_configured_remote_ollama_consent(
    lane_http, monkeypatch, tmp_path, consent,
):
    from sonder_runtime.adapters.inference import ollama_gateway
    seen = []

    def spawn(**kw):
        seen.append(kw["context"].remote_ollama_allowed)
        ollama_gateway._enforce_local_endpoint(_REMOTE_OLLAMA, kw["context"])
        return {"lane": {"id": kw["lane_id"], "workspace_root": kw["workspace_root"],
                         "parent_session_id": "p"}}

    config = _consent_app(monkeypatch, tmp_path, consent, spawn)
    assert config.ollama.allow_remote is (consent == "1")
    _lane_auth(monkeypatch)  # owner api key
    status, body = _lane_request(
        lane_http, "/v1/agent-lanes/delegate",
        {"task": "write primes.py", "command_id": "consent-" + consent},
    )
    assert seen == [consent == "1"]
    folders = _creation_folders(tmp_path / "state")
    if consent == "1":
        assert status == 202, body
        assert [str(p.resolve()) for p in folders] == [str(Path(body["delegation"]["folder"]).resolve())]
    else:
        assert status == 403, body
        assert body["error"]["code"] == "FORBIDDEN"
        assert "may leave this machine" in body["error"]["message"]
        assert folders == []


def test_http_delegate_account_never_inherits_remote_ollama_consent(
    lane_http, monkeypatch, tmp_path,
):
    _consent_app(
        monkeypatch, tmp_path, "1", lambda **kw: pytest.fail("a non-admin delegate reached spawn"),
    )
    assert serve._http_debug_context(_ACCOUNT, "t").remote_ollama_allowed is False
    _lane_auth(monkeypatch, account={"username": "reader", "role": "user"})
    status, body = _lane_request(lane_http, "/v1/agent-lanes/delegate", {"task": "write primes.py"})
    assert status == 403, body
    assert _creation_folders(tmp_path / "state") == []


# ---------------------------------------------------------------- MA-17 (e)

def _Ctx():
    from sonder_runtime.application.context import local_owner_context
    return local_owner_context(
        correlation_id="exercise-project-scope", source="http", session_id="chat-1",
    )


def _creation_folders(state):
    creations = state / "creations"
    return sorted(creations.glob("lane-*")) if creations.exists() else []


@pytest.mark.parametrize("expected", [
    PermissionError, ValueError, TypeError, Forbidden, InvalidInput, CapacityExceeded,
])
def test_refused_creation_spawn_leaves_no_lane_folder(tmp_path, expected):
    from sonder_runtime.interfaces.http.delegate import dispatch_delegate
    state = tmp_path / "state"
    calls = []

    def refuse(**kwargs):
        # The folder must exist when the lane is refused, so the empty listing
        # afterwards proves it was made and then removed, not never made.
        calls.append(_creation_folders(state))
        assert [p.resolve() for p in calls[-1]] == [Path(kwargs["workspace_root"]).resolve()]
        raise expected("refused")

    with pytest.raises(expected):
        dispatch_delegate(
            SimpleNamespace(spawn=refuse), {"task": "write primes.py", "command_id": "c1"},
            _Ctx(), state_home=state, allow_creation=True,
        )
    assert len(calls) == 1
    assert _creation_folders(state) == []


def test_uncertain_failure_keeps_the_lane_folder(tmp_path):
    from sonder_runtime.interfaces.http.delegate import dispatch_delegate
    state = tmp_path / "state"

    def transport_failure(**kwargs):
        raise RuntimeError("socket closed after the write")

    with pytest.raises(RuntimeError):
        dispatch_delegate(
            SimpleNamespace(spawn=transport_failure), {"task": "write primes.py", "command_id": "c2"},
            _Ctx(), state_home=state, allow_creation=True,
        )
    assert len(_creation_folders(state)) == 1


# ---------------------------------------------------------------- MA-16 (f)

@pytest.mark.parametrize("word,count", [("two", 2), ("three", 3), ("twelve", 12), ("5", 5)])
def test_number_word_worker_request_routes_to_fleet(tmp_path, word, count):
    repo = _repo_dir(tmp_path)
    prompt = "Spawn %s agents to review %s" % (word, repo)
    assert mo.requested_worker_cap(prompt) == count
    assert mo.requests_fleet(prompt) is True
    assert intents.classify_execution(prompt)["mode"] == "fleet"


def test_number_word_cue_keeps_its_guards():
    assert mo.requested_worker_cap("Do not spawn three agents") is None
    assert mo.requested_worker_cap('Explain why "spawn three agents" fails') is None
    assert mo.requested_worker_cap("Spawn three agents and then two agents") is None


@pytest.mark.parametrize("prompt", [
    "Use as many agents and tools as you can to audit and fix this repository",
    "Spawn as many subagents as you can to review the code",
])
def test_as_many_as_you_can_is_a_fleet_request(prompt):
    assert mo.requests_fleet(prompt) is True
    assert intents.classify_execution(prompt)["mode"] == "fleet"


# ---------------------------------------------------------------- MA-08 (g)

def test_repository_fleet_prompts_carry_review_angles_not_greenfield_ones():
    prompts = fleet_briefing.subtask_prompts(
        "Review the repository for defects", 3, tool_access=True, project="/repo",
    )
    for prompt in prompts:
        assert "Deliverable:" not in prompt
        assert "Contrarian take" not in prompt
        assert "Review angle:" in prompt


def test_greenfield_prompts_keep_their_angles():
    prompts = fleet_briefing.subtask_prompts("Invent a game", 2)
    assert all("Deliverable:" in prompt for prompt in prompts)


# ---------------------------------------------------------------- MA-15 (h)

def test_master_ack_receives_the_parsed_agent_count(monkeypatch):
    captured = []
    monkeypatch.setattr(serve, "_http_slash_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(serve, "_account_task_boundary_refusal", lambda *a, **kw: "")
    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", SimpleNamespace(
        master_orchestrator=SimpleNamespace(capacity=lambda: {"worker_slots": 1}),
        master_orchestrate=lambda **kw: "started",
        _http_chat_tools=lambda: SimpleNamespace(slash_run_arguments=lambda *a, **k: None),
    ))
    monkeypatch.setattr(
        serve, "_narrate_http_command",
        lambda name, arguments, call, context: captured.append((name, arguments)) or call(),
    )
    serve._handle_slash("/master fleet 2 review the repo", project="project")
    assert captured[0][1]["agents"] == 2
    captured.clear()
    serve._handle_slash("/master fleet review the repo", project="project")
    assert captured[0][1]["agents"] >= 1  # the same automatic count the run uses


def test_ack_text_says_two_agents():
    from sonder_runtime.bootstrap.http_command_narration import command_ack
    orchestrator = SimpleNamespace(
        capacity=lambda agents, worker_cap=None: {"worker_slots": 1, "requested_agents": agents},
        max_agents=lambda: 8, requested_worker_cap=lambda goal: None,
    )
    runtime = SimpleNamespace(master_orchestrator=orchestrator, _resolve_project=lambda p: p)
    ack = command_ack(runtime, "master_orchestrate",
                      {"task": "review", "mode": "fleet", "agents": 2})
    assert "fleet of 2 agents" in ack


def test_plan_labels_the_thirty_second_figure_as_a_default_guess():
    plan = fleet_briefing.format_plan("t", 4, 2, greenfield=False)
    assert "default guess" in plan


# ------------------------------------------------- MA-17 (e), continued

def test_retry_of_a_persisted_creation_command_keeps_its_folder(tmp_path):
    from sonder_runtime.interfaces.http.delegate import dispatch_delegate
    state = tmp_path / "state"
    calls = []

    def spawn(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            raise ValueError("command_id was used for a different request")
        return {"lane": {"id": kwargs["lane_id"], "workspace_root": kwargs["workspace_root"],
                         "parent_session_id": "p"}}

    payload = {"task": "write primes.py", "command_id": "c3"}
    dispatch_delegate(SimpleNamespace(spawn=spawn), payload, _Ctx(), state_home=state,
                      allow_creation=True)
    with pytest.raises(ValueError):
        dispatch_delegate(SimpleNamespace(spawn=spawn), payload, _Ctx(), state_home=state,
                          allow_creation=True)
    assert len(_creation_folders(state)) == 1
