"""Operator-driven ``/selfmod`` runs: candidate isolation and the stage journal.

``/selfmod run`` reaches ``server._selfmod_command`` from three surfaces: the
REPL console (``control_command(operator_approved=_console_has_operator())``),
the HTTP app chain (``sonder_serve._handle_slash``) and the MCP ``sonder`` tool
(``server.sonder``).  Every candidate check of such a run executes under the
isolation ``selfmod.operator_candidate_isolation`` selects -- the Linux uid
supervisor when ``SONDER_SELFMOD_CANDIDATE_UID`` is configured, Windows low
integrity on Windows -- and every mutating stage goes through the
bootstrap-composed selfmod stage journal.  A host with no candidate
supervisor refuses before anything exists, unless an attended console
operator typed ``--unisolated`` for this command; HTTP and MCP never can.

Only the editing model is replaced (``server._agent_impl`` writes the
candidate edit a model would have made).  The ledger, backup, workspace,
checks, journal and (for the root test) the Linux supervisor are real.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

import permission_modes as pm
import selfmod
import server
import sonder_runtime.interfaces.http.serve as sonder_serve
from scripts import selfmod_linux_isolation as linux

LINUX = sys.platform.startswith("linux")
ROOT = LINUX and hasattr(os, "geteuid") and os.geteuid() == 0
needs_root = pytest.mark.skipif(
    not ROOT, reason="Linux uid-separated selfmod supervisor needs Linux and euid 0",
)
needs_linux = pytest.mark.skipif(
    not LINUX, reason="the unconfigured-host refusal names the Linux candidate uid",
)
# A dedicated, otherwise unused uid for this test process (RLIMIT_NPROC is per
# real uid, so it must not be shared with another xdist worker).
CANDIDATE_UID = 290_000 + os.getpid() % 30_000

ORIGINAL = "VALUE = 1\n"
EDITED = "VALUE = 2\n"
TARGETED_TEST = "from sample import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=60,
    )
    return completed.stdout.strip()


@pytest.fixture(autouse=True)
def permission_sandbox(tmp_path, monkeypatch):
    """A tmp mode file, ``manual`` mode and no per-tool rule unless a test sets one."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode="manual", elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


def _allow_selfmod_rule(monkeypatch):
    """The written ``allow`` rule an unattended surface needs to reach ``/selfmod run``."""
    monkeypatch.setattr(
        pm, "_rule_lookup", lambda tool: {"pattern": tool, "action": pm.ALLOW, "note": "test"},
    )


@pytest.fixture
def host(monkeypatch):
    """A root-owned 0755 area: a Git checkout, selfmod state and a journal home."""
    area = Path(tempfile.mkdtemp(prefix="sonder-operator-selfmod-", dir="/tmp"))
    area.chmod(0o755)
    repo = area / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(ORIGINAL, encoding="utf-8")
    (repo / "test_sample.py").write_text(TARGETED_TEST, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "wiring@example.invalid")
    _git(repo, "config", "user.name", "wiring")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for path in [repo, *repo.rglob("*")]:
        if ".git" not in path.parts:
            path.chmod(0o755 if path.is_dir() else 0o644)
    home = area / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(area / "selfmod"))
    monkeypatch.delenv("SONDER_SELFMOD_DB", raising=False)
    monkeypatch.delenv("SELFMOD_LOW_INTEGRITY", raising=False)
    monkeypatch.delenv(linux.CANDIDATE_UID_ENV, raising=False)
    monkeypatch.delenv(linux.CANDIDATE_GID_ENV, raising=False)
    monkeypatch.setattr(server, "system_improvement_report", lambda: "measured sample defect")

    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    stages = build_application(
        config=SonderConfig(state=StateConfig(home=str(home))),
    ).selfmod_service()
    monkeypatch.setattr(server, "_selfmod_stage_journal", lambda: stages)
    selfmod.set_enabled(True)
    selfmod.set_mode("propose")
    try:
        yield {"area": area, "repo": repo, "home": home, "stages": stages}
    finally:
        if ROOT:
            linux._kill_uid(CANDIDATE_UID)
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree ") and Path(line[9:]) != repo:
                subprocess.run(["git", "worktree", "remove", "--force", line[9:]],
                               cwd=repo, capture_output=True, check=False)
        shutil.rmtree(area, ignore_errors=True)


def _editing_model(monkeypatch):
    """The model boundary: the editor writes the candidate edit into its workspace."""
    def edit(prompt, **_kwargs):
        workspace = Path(prompt.split("Workspace: ", 1)[1].splitlines()[0].strip())
        (workspace / "sample.py").write_text(EDITED, encoding="utf-8")
        return "edited sample.py"

    monkeypatch.setattr(server, "_agent_impl", edit)


def _run_text(*, flag: str = "") -> str:
    tests = "%s -m pytest -q -p no:cacheprovider test_sample.py" % sys.executable
    return "run raise VALUE to 2 --files sample.py --tests %s%s" % (
        tests, (" " + flag) if flag else "")


def _rows(run_id: str) -> list[sqlite3.Row]:
    with sqlite3.connect(selfmod.database_path()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT kind, passed, isolation, exit_code, output FROM selfmod_tests "
            "WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall()


def _journal(stages, run_id: str) -> dict[str, str]:
    binding = stages._effect_binding_factory(run_id)
    page = binding.journal.effects_since(binding.run_id, 0, limit=1000)
    return {record.operation_id: record.state.value for record in page.records}


def _only_run():
    runs = selfmod.list_runs(10)
    assert len(runs) == 1, runs
    return runs[0]


def _assert_fully_journaled(stages, run_id: str, *, checks: int) -> None:
    operations = _journal(stages, run_id)
    assert operations[f"selfmod-backup:{run_id}"] == "completed", operations
    assert operations[f"selfmod-prepare-workspace:{run_id}"] == "completed"
    assert operations[f"selfmod-reproducer-before:{run_id}:attempt-1"] == "completed"
    assert operations[f"selfmod-begin-testing:{run_id}:attempt-1"] == "completed"
    assert operations[f"selfmod-record-smoke:{run_id}:attempt-1"] == "completed"
    assert operations[f"selfmod-review:{run_id}"] == "completed"
    gates = sorted(op for op in operations if op.startswith(f"selfmod-record-test:{run_id}:"))
    assert gates == sorted(
        f"selfmod-record-test:{run_id}:attempt-{n}" for n in range(1, checks + 1))
    assert all(operations[op] == "completed" for op in gates), operations


# --- the decision itself ----------------------------------------------------


def test_a_host_with_a_supervisor_always_isolates(host, monkeypatch):
    monkeypatch.setattr(selfmod, "candidate_isolation_refusal", lambda: None)
    for requested in (False, True):
        for attended in (False, True):
            assert selfmod.operator_candidate_isolation(
                unisolated_requested=requested, operator_attended=attended) is True


@pytest.mark.parametrize("requested, attended, env, expected", [
    (False, True, "", "rerun /selfmod run with --unisolated"),
    (True, False, "", "requires an attended console operator"),
    (True, True, "1", "SELFMOD_LOW_INTEGRITY=1 requires isolated"),
])
def test_without_a_supervisor_only_an_attended_explicit_opt_in_runs(
    host, monkeypatch, requested, attended, env, expected,
):
    monkeypatch.setattr(selfmod, "candidate_isolation_refusal", lambda: "no supervisor here")
    if env:
        monkeypatch.setenv("SELFMOD_LOW_INTEGRITY", env)
    with pytest.raises(selfmod.CandidateIsolationRefused) as refused:
        selfmod.operator_candidate_isolation(
            unisolated_requested=requested, operator_attended=attended)
    assert expected in str(refused.value)
    assert refused.value.reason == "no supervisor here"
    assert isinstance(refused.value, PermissionError)


def test_an_auto_low_risk_candidate_never_runs_unisolated(host, monkeypatch):
    monkeypatch.setattr(selfmod, "candidate_isolation_refusal", lambda: "no supervisor here")
    selfmod.set_mode("auto-low-risk")
    run = selfmod.create_plan(
        "raise VALUE", host["repo"], evidence=["measured"], files=["sample.py"],
        criteria=["VALUE is 2"], risk="low",
    )
    with pytest.raises(selfmod.CandidateIsolationRefused, match="auto-low-risk"):
        selfmod.operator_candidate_isolation(
            unisolated_requested=True, operator_attended=True, run_id=run["id"])


# --- each surface on a host without a candidate supervisor ------------------


@needs_linux
def test_repl_refuses_an_unisolated_run_it_was_not_explicitly_asked_for(host, monkeypatch):
    """Console approval of ``/selfmod run`` alone is not consent to run unisolated."""
    monkeypatch.setattr(server, "_agent_impl",
                        lambda *_a, **_k: pytest.fail("editor reached without isolation"))
    out = server.control_command("/selfmod " + _run_text(), operator_approved=True)
    assert out.startswith("refused /selfmod run: candidate isolation unavailable"), out
    assert linux.CANDIDATE_UID_ENV in out
    assert linux.ISOLATION_DOC in out
    assert "--unisolated" in out
    assert selfmod.list_runs(10) == []


@needs_linux
def test_a_piped_console_cannot_opt_out_of_isolation(host, monkeypatch):
    import sonder_runtime.interfaces.repl.repl as sonder_repl

    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    _allow_selfmod_rule(monkeypatch)
    out = server.control_command(
        "/selfmod " + _run_text(flag="--unisolated"),
        operator_approved=sonder_repl._console_has_operator(),
    )
    assert out.startswith("refused /selfmod run:"), out
    assert "requires an attended console operator" in out
    assert selfmod.list_runs(10) == []


@needs_linux
def test_http_cannot_opt_out_of_isolation(host, monkeypatch):
    """Even with the written allow rule that lets HTTP reach ``/selfmod run``."""
    _allow_selfmod_rule(monkeypatch)
    for text in (_run_text(), _run_text(flag="--unisolated")):
        out = sonder_serve._handle_slash("/selfmod " + text)
        assert out.startswith("refused /selfmod run: candidate isolation unavailable"), out
        assert linux.CANDIDATE_UID_ENV in out
    assert "requires an attended console operator" in out
    assert selfmod.list_runs(10) == []


@needs_linux
def test_mcp_cannot_opt_out_of_isolation(host, monkeypatch):
    """The MCP ``sonder`` tool reaches ``control_command`` with nobody attending."""
    _allow_selfmod_rule(monkeypatch)
    for text in (_run_text(), _run_text(flag="--unisolated")):
        out = server.sonder("/selfmod " + text)
        assert out.startswith("refused /selfmod run: candidate isolation unavailable"), out
    assert "requires an attended console operator" in out
    assert selfmod.list_runs(10) == []


@needs_linux
def test_an_attended_explicit_opt_in_runs_unisolated_and_journaled(host, monkeypatch):
    """The one way to run a candidate on a host without a supervisor.

    ``_selfmod_command`` is what ``control_command`` forwards to; it is called
    directly only to point the run at the fixture checkout instead of Sonder's.
    """
    _editing_model(monkeypatch)
    out = server._selfmod_command(
        _run_text(flag="--unisolated"), repository_root=host["repo"], operator_approved=True,
    )
    run = _only_run()
    assert run["phase"] == "reviewing", (out, [dict(r) for r in _rows(run["id"])])
    rows = _rows(run["id"])
    assert [row["kind"] for row in rows] == [
        "reproducer_before", "syntax", "targeted", "regression", "smoke"]
    # Nothing claims an isolation boundary it did not have.
    assert {row["isolation"] for row in rows} == {"unverified"}
    assert all(row["passed"] == 1 for row in rows), [dict(r) for r in rows]
    events = selfmod.events(run["id"])
    isolation = [event for event in events if event["kind"] == "isolation"]
    assert len(isolation) == 1
    assert "attended console operator accepted an unisolated candidate" in isolation[0]["details"]
    _assert_fully_journaled(host["stages"], run["id"], checks=3)
    assert (host["repo"] / "sample.py").read_text(encoding="utf-8") == ORIGINAL


def test_an_isolated_host_needs_no_attendance_and_isolates_every_check(host, monkeypatch):
    """HTTP (no operator) proceeds when the host has a supervisor, and asks for it."""
    monkeypatch.setattr(selfmod, "candidate_isolation_refusal", lambda: None)
    _allow_selfmod_rule(monkeypatch)
    seen = []
    real_record = selfmod._record_command

    def spy(run, kind, command, cwd_path, seconds, **kwargs):
        seen.append((kind, kwargs.get("low_integrity"), tuple(kwargs.get("protected_paths") or ())))
        return real_record(run, kind, command, cwd_path, seconds, **kwargs)

    monkeypatch.setattr(selfmod, "_record_command", spy)
    _editing_model(monkeypatch)
    out = server._selfmod_command(_run_text(), repository_root=host["repo"])
    run = _only_run()
    candidate = [entry for entry in seen if entry[0] != "reproducer_before"]
    assert [entry[0] for entry in candidate] == ["syntax", "targeted", "regression", "smoke"], out
    backup = str(selfmod._backup_dir(run["id"]))
    for _kind, low_integrity, protected in candidate:
        assert low_integrity is True
        assert backup in protected
    events = [event["details"] for event in selfmod.events(run["id"]) if event["kind"] == "isolation"]
    assert events == ["candidate checks run under the selected candidate supervisor"]


def test_approve_deploy_and_rollback_are_journaled(host, monkeypatch):
    """The other operator stages go through the same journal identities."""
    stages = host["stages"]
    run = selfmod.create_plan(
        "raise VALUE", host["repo"], evidence=["measured"], files=["sample.py"],
        criteria=["VALUE is 2"],
    )
    rid = run["id"]
    # A known run in the wrong phase is refused before a one-shot intent exists.
    out = server._selfmod_command("deploy %s" % rid, operator_approved=True)
    assert out == "refused /selfmod deploy: run %s is proposed; it requires phase approved" % rid
    out = server._selfmod_command("rollback %s" % rid, operator_approved=True)
    assert out.startswith("refused /selfmod rollback:") and "requires phase deployed" in out
    assert _journal(stages, rid) == {}

    # The ledger phases are real (the journal's phase precondition reads
    # them); the reviewing state is set directly because reaching it for real
    # is the run path covered above.  Approval itself is the real one.
    selfmod._phase(rid, {"proposed"}, "reviewing", "test", "fixture: reviewed")
    monkeypatch.setattr(selfmod, "format_run", lambda run_id: "run %s" % run_id)
    assert server._selfmod_command("approve %s" % rid) == "run %s" % rid
    assert selfmod.get_run(rid)["approved_by"] == "explicit local/developer user"
    assert _journal(stages, rid)[f"selfmod-approve:{rid}"] == "completed"

    # Deploy and rollback rewrite a live tree; their legacy bodies are
    # covered by tests/test_selfmod*.py.  Here they are recorded stand-ins
    # that move the real ledger the way the real ones do.
    def deploy(run_id, **_kwargs):
        return selfmod._phase(run_id, {"approved"}, "deployed", "test", "fixture: deployed")

    def rollback(run_id, reason=""):
        return selfmod._phase(run_id, {"deployed"}, "restored", "test", reason)

    monkeypatch.setattr(selfmod, "deploy", deploy)
    monkeypatch.setattr(selfmod, "rollback", rollback)
    assert server._selfmod_command("deploy %s" % rid, operator_approved=True) == "run %s" % rid
    assert _journal(stages, rid)[f"selfmod-deploy:{rid}"] == "completed"
    assert server._selfmod_command("rollback %s" % rid, operator_approved=True) == "run %s" % rid
    assert _journal(stages, rid)[f"selfmod-rollback:{rid}"] == "completed"


def test_no_stage_journal_means_no_mutation(host, monkeypatch):
    def unavailable():
        raise RuntimeError("no graph")

    monkeypatch.setattr(server, "_selfmod_stage_journal", unavailable)
    monkeypatch.setattr(selfmod, "candidate_isolation_refusal", lambda: None)
    monkeypatch.setattr(server, "_agent_impl",
                        lambda *_a, **_k: pytest.fail("editor reached without a journal"))
    out = server._selfmod_command(_run_text(), repository_root=host["repo"])
    assert out.startswith("refused /selfmod run: selfmod stage journal unavailable"), out
    assert _only_run()["phase"] == "proposed"
    assert not selfmod._backup_dir(_only_run()["id"]).exists()
    out = server._selfmod_command("approve %s" % _only_run()["id"])
    assert out.startswith("refused /selfmod approve: selfmod stage journal unavailable"), out


# --- the real boundary --------------------------------------------------------


@needs_root
def test_repl_run_is_isolated_under_the_uid_supervisor_and_journaled(host, monkeypatch):
    """A configured Linux host: every candidate check runs as the candidate uid."""
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    # The fixture's state home was created 0700 before the uid was configured.
    # The documented one-time operator step makes it traverse-only (0711) so
    # the candidate uid can reach its workspace under selfmod/workspaces.
    host["home"].chmod(0o711)
    _editing_model(monkeypatch)
    head = _git(host["repo"], "rev-parse", "HEAD")

    out = server._selfmod_command(
        _run_text(), repository_root=host["repo"], operator_approved=True,
    )

    run = _only_run()
    rows = _rows(run["id"])
    assert run["phase"] == "reviewing", (out, [(r["kind"], r["output"][-1500:]) for r in rows])
    by_kind = {row["kind"]: row for row in rows}
    assert set(by_kind) == {"reproducer_before", "syntax", "targeted", "regression", "smoke"}
    # The reproducer runs the declared check against the untouched live
    # source, not candidate bytes; every candidate check is uid-isolated.
    assert by_kind["reproducer_before"]["isolation"] == "unverified"
    for kind in ("syntax", "targeted", "regression", "smoke"):
        row = by_kind[kind]
        assert row["isolation"] == "linux-uid", dict(row)
        assert row["passed"] == 1, dict(row)
        assert '"integrity": "linux-uid"' in row["output"]
        assert f'"uid": {CANDIDATE_UID}' in row["output"]
        assert '"supervisor_uid": 0' in row["output"]
    _assert_fully_journaled(host["stages"], run["id"], checks=3)
    assert _git(host["repo"], "rev-parse", "HEAD") == head
    assert (host["repo"] / "sample.py").read_text(encoding="utf-8") == ORIGINAL
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


@needs_root
def test_repl_run_candidate_cannot_write_the_live_checkout(host, monkeypatch):
    """A candidate that writes the live checkout on import is denied and rejected."""
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    host["home"].chmod(0o711)
    live = host["repo"] / "sample.py"
    tamper = (
        "import pathlib\n"
        f"pathlib.Path({str(live)!r}).write_text('tampered')\n"
        "VALUE = 2\n"
    )

    def edit(prompt, **_kwargs):
        workspace = Path(prompt.split("Workspace: ", 1)[1].splitlines()[0].strip())
        (workspace / "sample.py").write_text(tamper, encoding="utf-8")
        return "edited sample.py"

    monkeypatch.setattr(server, "_agent_impl", edit)
    server._selfmod_command(_run_text(), repository_root=host["repo"], operator_approved=True)

    run = _only_run()
    assert run["phase"] in {"rejected", "restored"}, run["phase"]
    rows = {row["kind"]: row for row in _rows(run["id"])}
    targeted = rows["targeted"]
    assert targeted["isolation"] == "linux-uid"
    assert targeted["passed"] == 0
    assert "Permission denied" in targeted["output"]
    assert live.read_text(encoding="utf-8") == ORIGINAL
    operations = _journal(host["stages"], run["id"])
    failed = [op for op, state in operations.items()
              if op.startswith(f"selfmod-record-test:{run['id']}:") and state == "failed"]
    assert failed, operations
    # A rejected candidate is a settled outcome, never an uncertain effect.
    assert "uncertain" not in operations.values(), operations
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


# --- the production journal composition ---------------------------------------


def test_the_operator_path_composes_the_production_stage_journal(host, tmp_path, monkeypatch):
    """``server._selfmod_stage_journal`` itself, not a test replacement.

    Every other test here swaps it for a per-test journal.  This one lets the
    server build its own application graph (hermetic state home) and drives a
    real ``/selfmod approve`` through it, so a rename or a composition failure
    in the bootstrap selfmod service fails here instead of silently refusing
    every production operator stage.
    """
    from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding
    from sonder_runtime.application.selfmod.selfmod_service import GuardedLegacySelfmodService

    monkeypatch.undo()  # drop the fixture's replacement and environment ...
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(host["area"] / "selfmod"))  # ... keep its ledger
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SONDER_WORKER_EFFECTS_DB", str(tmp_path / "worker-effects.db"))
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "_APP_GRAPH_OWNED_BY_SERVER", False)
    try:
        stages = server._selfmod_stage_journal()
        assert isinstance(stages, GuardedLegacySelfmodService)
        run = selfmod.create_plan(
            "raise VALUE", host["repo"], evidence=["measured"], files=["sample.py"],
            criteria=["VALUE is 2"],
        )
        rid = run["id"]
        binding = stages._effect_binding_factory(rid)
        assert isinstance(binding, AuthenticatedWorkerBinding)
        assert binding.scope == "selfmod-mutation"
        assert binding.run_id == "selfmod:%s" % rid

        selfmod._phase(rid, {"proposed"}, "reviewing", "test", "fixture: reviewed")
        out = server._selfmod_command("approve %s" % rid)
        assert selfmod.get_run(rid)["phase"] == "approved", out
        assert _journal(server._selfmod_stage_journal(), rid) == {
            f"selfmod-approve:{rid}": "completed"}
        # The server built (and owns) the graph that journaled the stage, and
        # its journal is a hermetic state home's, never the suite's shared one.
        assert server._APP_GRAPH is not None and server._APP_GRAPH_OWNED_BY_SERVER
        journal_path = Path(binding.journal.database_path).resolve()
        assert journal_path.name == "worker-effects.db"
        assert any(root.resolve() in journal_path.parents
                   for root in (tmp_path, host["area"])), journal_path
    finally:
        server._close_server_owned_application()


# --- operator stages: serialization and benign refusals ---------------------


def _approved_run(host) -> str:
    run = selfmod.create_plan(
        "raise VALUE", host["repo"], evidence=["measured"], files=["sample.py"],
        criteria=["VALUE is 2"],
    )
    selfmod._phase(run["id"], {"proposed"}, "reviewing", "test", "fixture: reviewed")
    selfmod._phase(run["id"], {"reviewing"}, "approved", "test", "fixture: approved")
    return run["id"]


def test_a_concurrent_second_deploy_is_refused_and_the_first_completes(host, monkeypatch):
    """A double submit no longer fences the run it was meant to deploy.

    The second call finds the run's owner lease held and refuses before it
    composes a journal binding, so the first call's in-flight intent is never
    treated as orphaned; the first deploy settles ``completed`` and the run
    can still be rolled back.
    """
    stages = host["stages"]
    rid = _approved_run(host)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def deploy(run_id, **_kwargs):
        calls.append(run_id)
        entered.set()
        assert release.wait(30)
        return selfmod._phase(run_id, {"approved"}, "deployed", "test", "fixture: deployed")

    def rollback(run_id, reason=""):
        return selfmod._phase(run_id, {"deployed"}, "restored", "test", reason)

    monkeypatch.setattr(selfmod, "deploy", deploy)
    monkeypatch.setattr(selfmod, "rollback", rollback)
    monkeypatch.setattr(selfmod, "format_run", lambda run_id: "run %s" % run_id)
    first = []
    worker = threading.Thread(
        target=lambda: first.append(
            server._selfmod_command("deploy %s" % rid, operator_approved=True)),
        daemon=True,
    )
    worker.start()
    assert entered.wait(30)
    try:
        second = server._selfmod_command("deploy %s" % rid, operator_approved=True)
        assert second.startswith("refused /selfmod deploy: run %s is already being driven" % rid), second
        # So is any other operator stage of the same run meanwhile.
        other = server._selfmod_command("rollback %s" % rid, operator_approved=True)
        assert other.startswith("refused /selfmod rollback: run %s is already being driven" % rid), other
    finally:
        release.set()
        worker.join(30)
    assert first == ["run %s" % rid]
    assert calls == [rid]
    assert selfmod.get_run(rid)["phase"] == "deployed"
    assert selfmod.get_run(rid)["owner_id"] is None  # lease released
    assert _journal(stages, rid) == {f"selfmod-deploy:{rid}": "completed"}
    assert server._selfmod_command("rollback %s" % rid, operator_approved=True) == "run %s" % rid
    assert _journal(stages, rid) == {
        f"selfmod-deploy:{rid}": "completed", f"selfmod-rollback:{rid}": "completed"}


def test_a_run_leased_by_another_process_is_refused_before_the_journal(host):
    """The lease is the ledger's, so it serializes across processes too."""
    stages = host["stages"]
    run = selfmod.create_plan(
        "raise VALUE", host["repo"], evidence=["measured"], files=["sample.py"],
        criteria=["VALUE is 2"],
    )
    rid = run["id"]
    selfmod._phase(rid, {"proposed"}, "reviewing", "test", "fixture: reviewed")
    owner = selfmod.claim(rid)
    try:
        out = server._selfmod_command("approve %s" % rid)
        assert out.startswith("refused /selfmod approve: run %s is already being driven" % rid), out
        assert selfmod.get_run(rid)["phase"] == "reviewing"
        assert _journal(stages, rid) == {}
    finally:
        selfmod.release(rid, owner)
    server._selfmod_command("approve %s" % rid)
    assert selfmod.get_run(rid)["phase"] == "approved"


@pytest.mark.parametrize("text", [
    "deploy " + "x" * 5000,
    "rollback no-such-run trailing words",
    "approve no-such-run",
], ids=["overlong-deploy", "rollback-trailing-words", "approve-typo"])
def test_an_unknown_run_is_refused_without_a_journal_record(host, text):
    """A typo'd id never becomes a permanent uncertain journal identity."""
    stages = host["stages"]
    action, _, rid = text.partition(" ")
    out = server._selfmod_command(text, operator_approved=True)
    assert out.startswith("refused /selfmod %s: unknown selfmod run " % action), out
    assert len(out) < 300
    # The binding composes cleanly: nothing uncertain was left for this id.
    assert _journal(stages, rid) == {}


def _lock_deployments_as_a_live_owner() -> None:
    import socket

    with sqlite3.connect(selfmod.database_path()) as conn:
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id=?,owner_pid=?,owner_host=?,"
            "lease_until=?,run_id=? WHERE id=1",
            ("other-deployer", os.getpid(), socket.gethostname(), 4e9, "other-run"),
        )


def _unlock_deployments() -> None:
    with sqlite3.connect(selfmod.database_path()) as conn:
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id=NULL,owner_pid=NULL,"
            "owner_host=NULL,lease_until=NULL,run_id=NULL WHERE id=1")


@needs_linux
def test_benign_deploy_and_rollback_refusals_can_be_retried(host, monkeypatch):
    """Real legacy deploy/rollback: a refusal before any write never fences the run.

    ``deploy`` refused because another deployment holds the lock, and
    ``rollback`` refused because the deployed file was edited afterwards,
    both settle ``failed`` with a ``:not-applied`` receipt; once the operator
    clears the cause, the retry reaches the legacy function, deploys the
    tested bytes and rolls them back.
    """
    stages = host["stages"]
    repo = host["repo"]
    # The deploy health command imports ``server``; this checkout's stands in.
    (repo / "server.py").write_text("def status():\n    return 'ok'\n", encoding="utf-8")
    (repo / "server.py").chmod(0o644)
    _git(repo, "add", "server.py")
    _git(repo, "commit", "-q", "-m", "health stand-in")
    _editing_model(monkeypatch)
    server._selfmod_command(
        _run_text(flag="--unisolated"), repository_root=repo, operator_approved=True,
    )
    rid = _only_run()["id"]
    assert selfmod.get_run(rid)["phase"] == "reviewing"
    server._selfmod_command("approve %s" % rid)
    assert selfmod.get_run(rid)["phase"] == "approved"

    _lock_deployments_as_a_live_owner()
    out = server._selfmod_command("deploy %s" % rid, operator_approved=True)
    assert out == "ERROR: another deployment/rollback holds the process-safe lock", out
    assert selfmod.get_run(rid)["phase"] == "approved"
    assert (repo / "sample.py").read_text(encoding="utf-8") == ORIGINAL
    binding = stages._effect_binding_factory(rid)  # composes: nothing uncertain
    refused = binding.journal.get(f"{binding.run_id}:selfmod-deploy:{rid}")
    assert refused.state.value == "failed"
    assert refused.receipt_key == f"selfmod:{rid}:deploy:not-applied"

    _unlock_deployments()
    out = server._selfmod_command("deploy %s" % rid, operator_approved=True)
    assert selfmod.get_run(rid)["phase"] == "deployed", out
    assert (repo / "sample.py").read_text(encoding="utf-8") == EDITED
    assert _journal(stages, rid)[f"selfmod-deploy:{rid}:retry-2"] == "completed"

    (repo / "sample.py").write_text("VALUE = 3  # operator edit\n", encoding="utf-8")
    out = server._selfmod_command("rollback %s" % rid, operator_approved=True)
    assert out.startswith("ERROR: rollback conflict: deployed files changed"), out
    assert selfmod.get_run(rid)["phase"] == "deployed"
    (repo / "sample.py").write_text(EDITED, encoding="utf-8")
    out = server._selfmod_command("rollback %s" % rid, operator_approved=True)
    assert selfmod.get_run(rid)["phase"] == "restored", out
    assert (repo / "sample.py").read_text(encoding="utf-8") == ORIGINAL

    operations = _journal(stages, rid)
    assert operations[f"selfmod-deploy:{rid}"] == "failed"
    assert operations[f"selfmod-rollback:{rid}"] == "failed"
    assert operations[f"selfmod-rollback:{rid}:retry-2"] == "completed"
    assert "uncertain" not in operations.values(), operations
    assert "intent" not in operations.values(), operations


def test_a_stage_failure_after_mutation_still_fences_the_run(host, monkeypatch):
    """Only the typed pre-mutation refusal is retryable; anything else stays fenced."""
    stages = host["stages"]
    rid = _approved_run(host)

    def deploy(run_id, **_kwargs):
        raise RuntimeError("installed bytes differ from tested bytes: sample.py")

    monkeypatch.setattr(selfmod, "deploy", deploy)
    out = server._selfmod_command("deploy %s" % rid, operator_approved=True)
    assert out == "ERROR: installed bytes differ from tested bytes: sample.py"
    out = server._selfmod_command("deploy %s" % rid, operator_approved=True)
    assert out == "ERROR: worker restart requires explicit reconciliation of uncertain effects"


# --- HTTP and MCP drive a real isolated run ---------------------------------


@needs_root
@pytest.mark.parametrize("surface", ["http", "mcp"])
def test_an_unattended_surface_runs_an_isolated_journaled_candidate(host, monkeypatch, surface):
    """With a candidate supervisor, HTTP and MCP need no attendance -- and get isolation.

    Driven through ``sonder_serve._handle_slash`` / ``server.sonder`` with the
    written allow rule an unattended surface needs; only the repository root
    is redirected to the fixture checkout (``control_command`` passes none).
    """
    import functools

    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    host["home"].chmod(0o711)
    _allow_selfmod_rule(monkeypatch)
    _editing_model(monkeypatch)
    monkeypatch.setattr(server, "_selfmod_command", functools.partial(
        server._selfmod_command, repository_root=host["repo"]))
    command = "/selfmod " + _run_text()
    out = sonder_serve._handle_slash(command) if surface == "http" else server.sonder(command)

    run = _only_run()
    rows = {row["kind"]: row for row in _rows(run["id"])}
    assert run["phase"] == "reviewing", (out, {k: r["output"][-800:] for k, r in rows.items()})
    for kind in ("syntax", "targeted", "regression", "smoke"):
        assert rows[kind]["isolation"] == "linux-uid", dict(rows[kind])
        assert rows[kind]["passed"] == 1
    events = [e["details"] for e in selfmod.events(run["id"]) if e["kind"] == "isolation"]
    assert events == ["candidate checks run under the selected candidate supervisor"]
    _assert_fully_journaled(host["stages"], run["id"], checks=3)
    assert (host["repo"] / "sample.py").read_text(encoding="utf-8") == ORIGINAL
    assert linux.live_uid_pids(CANDIDATE_UID) == set()
