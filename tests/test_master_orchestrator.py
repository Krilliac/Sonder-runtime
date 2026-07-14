import importlib
import sys
import threading
import time
from types import SimpleNamespace

import master_orchestrator


def _repository_receipt(project, output="grounded result"):
    return SimpleNamespace(
        output=(
            output
            + "\n\n=== TOOL EVIDENCE ===\nstep 1 tool=file_read\nscoped source"
        ),
        tools=("file_read",),
        project_scope=str(project),
    )


def setup_function():
    master_orchestrator.reset_for_tests()


def test_evidence_gate_rejects_repo_inspection_when_tools_unavailable():
    task = "Repository: D:\\SparkEngine. Review current uncommitted files using local file-reading tools."

    assert master_orchestrator.evidence_gate(task, tools_available=False) == master_orchestrator.EVIDENCE_REQUIRED
    assert master_orchestrator.evidence_gate(task, tools_available=True) == ""


def test_evidence_gate_allows_embedded_source_excerpt():
    task = "Review the current file. Source excerpt:\n```cpp\nint answer() { return 42; }\n```"

    assert master_orchestrator.evidence_gate(task) == ""


def test_greenfield_design_is_not_treated_as_repository_inspection():
    task = "Design and implement a C++ 2.5D isometric RPG from scratch."

    assert not master_orchestrator.requires_repository_tools(task)


def test_delegated_prompts_disclose_no_tool_access_and_demand_evidence():
    prompt = master_orchestrator._subtask_prompts("compare these excerpts", 1)[0]

    assert "no filesystem, shell, web" in prompt
    assert "Quote the exact supporting excerpt" in prompt
    assert "EVIDENCE_REQUIRED" in prompt


def test_repository_prompts_require_guarded_read_tools():
    prompt = master_orchestrator._subtask_prompts(
        "Repository: D:\\SparkEngine. Inspect current files.",
        1,
        tool_access=True,
    )[0]

    assert "guarded read-only file tools" in prompt
    assert "never request write/edit/delete tools" in prompt


def test_tool_equipped_prompts_are_not_primed_to_bail_with_evidence_required():
    # Regression (2026-07-13): the tool-access prompt carried the no-tools
    # branch's "if repository evidence is absent, answer EVIDENCE_REQUIRED"
    # line. A local model holding real file tools took that bait and finalized
    # on step 1 without calling them; the host then rejected the toolless answer
    # (require_file_evidence), so EVERY delegated repository task came back
    # EVIDENCE_REQUIRED. A tool-equipped agent must be told to go read first.
    tooled = master_orchestrator._subtask_prompts("Inspect the repo files.", 1, tool_access=True)[0]

    assert "USE THEM" in tooled
    assert "BEFORE answering" in tooled
    # It may still surrender AFTER genuinely trying -- but only after trying.
    assert "actually tried" in tooled

    # The no-tools branch keeps the original refuse-without-evidence contract.
    toolless = master_orchestrator._subtask_prompts("compare these excerpts", 1)[0]
    assert "EVIDENCE_REQUIRED" in toolless
    assert "no filesystem, shell, web" in toolless


def test_repository_worker_arms_the_inspect_before_final_nudge(monkeypatch, tmp_path):
    # auto_checklist is what arms the host's "use an inspection tool before you
    # finalize" retry. Without it the repository lane was one-shot and failed
    # any model that did not call a tool on its very first step.
    captured = {}

    class _FakeServer:
        def _agent_impl(self, prompt, **kwargs):
            captured.update(kwargs)
            return _repository_receipt(tmp_path)

    monkeypatch.setitem(sys.modules, "server", _FakeServer())

    result = master_orchestrator._repository_worker(
        "inspect the repo", project=str(tmp_path),
    )
    assert isinstance(result, master_orchestrator.RepositoryWorkerResult)
    assert captured["auto_checklist"] is True
    assert captured["require_file_evidence"] is True
    assert captured["read_only"] is True


def test_repository_worker_propagates_labeled_external_project(monkeypatch, tmp_path):
    captured = {}

    class _FakeServer:
        def _agent_impl(self, prompt, **kwargs):
            captured.update(kwargs)
            return _repository_receipt(tmp_path)

    monkeypatch.setitem(sys.modules, "server", _FakeServer())
    task = "Repository: %s. Read-only implementation review." % tmp_path

    result = master_orchestrator._repository_worker(task)
    assert isinstance(result, master_orchestrator.RepositoryWorkerResult)
    assert captured["project"] == str(tmp_path.resolve())


def test_repository_project_root_uses_absolute_file_parent(tmp_path):
    source = tmp_path / "src" / "main.cpp"
    source.parent.mkdir()
    source.write_text("int main() {}\n", encoding="utf-8")

    assert master_orchestrator.repository_project_root(
        "summarize the file %s" % source
    ) == str(source.parent.resolve())


def test_run_inline_tracks_master_agent():
    result = master_orchestrator.run_inline("say hi", lambda prompt: "done: " + prompt)
    snap = master_orchestrator.snapshot()

    assert result["mode"] == "inline"
    assert result["output"] == "done: say hi"
    assert any(a["id"] == result["master_id"] and a["status"] == "done" for a in snap["agents"])


def test_run_delegated_tracks_children_and_audit():
    def worker(prompt):
        return "worker saw " + prompt.splitlines()[-1]

    def audit(prompt):
        assert "worker saw" in prompt
        assert "Discard invented files, symbols, APIs" in prompt
        return "merged"

    result = master_orchestrator.run_delegated(
        "compare options",
        worker_fn=worker,
        audit_fn=audit,
        agents=2,
    )
    snap = master_orchestrator.snapshot()

    assert result["mode"] == "delegated"
    assert result["output"] == "merged"
    assert len(result["agents"]) == 2
    assert snap["active_agents"] == 0
    assert snap["tokens_in"] > 0
    assert snap["latest_master_result"] == "merged"
    formatted = master_orchestrator.format_snapshot(snap)
    assert "latest completed master result [" in formatted
    assert "  task: compare options\nmerged" in formatted


def test_status_labels_prior_result_while_an_unrelated_fleet_is_active():
    completed = master_orchestrator.run_inline(
        "Summarize the previous Spark font audit.",
        lambda prompt: "old Spark result",
    )

    # Exercise formatting directly with a live row because a synchronous unit
    # worker cannot remain active after run_inline returns.
    data = master_orchestrator.snapshot()
    data["active_agents"] = 2
    data["agents"] = [{
        "id": "master-new",
        "status": "running",
        "activity": "auditing D:\\smellslikenapalm",
        "task": "Project: D:\\smellslikenapalm",
    }]
    formatted = master_orchestrator.format_snapshot(data)

    assert completed["master_id"] in formatted
    assert "task: Summarize the previous Spark font audit." in formatted
    assert "completed history; this is not the result of the active agents above" in formatted
    assert "old Spark result" in formatted


def test_all_failed_workers_fail_master_and_skip_audit():
    audited = []

    def fail(_prompt):
        raise RuntimeError("backend unavailable")

    result = master_orchestrator.run_delegated(
        "compare options",
        worker_fn=fail,
        audit_fn=lambda prompt: audited.append(prompt) or "must not run",
        agents=2,
    )
    snap = master_orchestrator.snapshot()
    master = next(row for row in snap["agents"] if row["role"] == "master")
    children = [row for row in snap["agents"] if row["role"] == "agent"]

    assert result["outputs"] == []
    assert "all delegated workers failed" in result["output"]
    assert master["status"] == "failed"
    assert {row["status"] for row in children} == {"failed"}
    assert audited == []


def test_partial_fleet_audits_successful_outputs_only():
    audited = []

    def worker(prompt):
        if "subagent 1/2" in prompt:
            raise RuntimeError("first worker failed")
        return "usable worker result"

    def audit(prompt):
        audited.append(prompt)
        assert "usable worker result" in prompt
        assert "first worker failed" not in prompt
        return "merged success"

    result = master_orchestrator.run_delegated(
        "compare options", worker_fn=worker, audit_fn=audit, agents=2,
    )
    snap = master_orchestrator.snapshot()

    assert result["output"] == "merged success"
    assert len(result["outputs"]) == 1
    assert len(audited) == 1
    assert any(row["status"] == "failed" for row in snap["agents"])
    assert any(
        row["role"] == "master" and row["status"] == "done"
        for row in snap["agents"]
    )


def test_repository_delegation_refuses_outputs_without_tool_ledger(monkeypatch):
    audited = []

    result = master_orchestrator.run_delegated(
        "Repository: D:\\SparkEngine. Inspect current files.",
        worker_fn=lambda prompt, project: "I inspected it and everything passes.",
        audit_fn=lambda prompt: audited.append(prompt) or "should not run",
        agents=2,
    )

    assert result["output"] == master_orchestrator.EVIDENCE_REQUIRED
    assert result["outputs"] == []
    assert audited == []


def test_repository_fleet_propagates_exact_project_and_scopes_aggregation(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda count: 1)
    worker_projects = []
    audit_prompts = []

    def worker(_prompt, project):
        worker_projects.append(project)
        return master_orchestrator.repository_worker_result(
            _repository_receipt(project), project,
        )

    result = master_orchestrator.run_delegated(
        "Audit current source files.",
        worker_fn=worker,
        audit_fn=lambda prompt: audit_prompts.append(prompt) or "scoped merge",
        agents=2,
        project=str(tmp_path),
    )
    snapshot = master_orchestrator.snapshot(limit=20)
    rows = [
        row for row in snapshot["agents"]
        if row["id"] == result["master_id"] or row["parent_id"] == result["master_id"]
    ]

    expected = str(tmp_path.resolve())
    assert worker_projects == [expected, expected]
    assert rows and {row["project"] for row in rows} == {expected}
    assert all(row["files"] == [expected] for row in rows)
    assert "project: %s" % expected in master_orchestrator.format_snapshot(snapshot)
    assert "HOST REPOSITORY SCOPE: %s" % expected in audit_prompts[0]
    assert "This is repository work, not greenfield design" in audit_prompts[0]
    assert result["output"].startswith("=== HOST AGGREGATION SCOPE ===")
    assert "project=%s" % expected in result["output"]


def test_repository_fleet_rejects_scope_receipt_from_another_project(tmp_path):
    requested = tmp_path / "requested"
    wrong = tmp_path / "wrong"
    requested.mkdir()
    wrong.mkdir()
    audited = []

    result = master_orchestrator.run_delegated(
        "Inspect current source files.",
        worker_fn=lambda _prompt, _project: master_orchestrator.RepositoryWorkerResult(
            output="fake\n\n=== TOOL EVIDENCE ===\nsource",
            project=str(wrong),
            tools=("file_read",),
        ),
        audit_fn=lambda prompt: audited.append(prompt) or "must not run",
        agents=1,
        project=str(requested),
    )

    assert result["output"] == master_orchestrator.EVIDENCE_REQUIRED
    assert audited == []
    child = next(
        row for row in master_orchestrator.snapshot(limit=10)["agents"]
        if row["role"] == "agent"
    )
    assert child["status"] == "failed"
    assert "escaped its assigned project scope" in child["error"]


def test_repository_scope_never_falls_back_to_process_cwd():
    try:
        master_orchestrator.resolve_repository_project_root(
            "Inspect the current repository files."
        )
    except ValueError as exc:
        assert "no cwd fallback" in str(exc)
    else:
        raise AssertionError("repository work silently inherited the process cwd")


def test_run_delegated_default_cap_allows_sixteen_agents(monkeypatch):
    monkeypatch.delenv("SONDER_MAX_AGENTS", raising=False)

    result = master_orchestrator.run_delegated(
        "fan out",
        worker_fn=lambda prompt: "ok",
        audit_fn=lambda prompt: "merged",
        agents=99,
    )

    assert master_orchestrator.max_agents() == master_orchestrator.hardware_max_agents()
    assert len(result["agents"]) == master_orchestrator.hardware_max_agents()


def test_fleet_keywords_request_hardware_fanout():
    assert master_orchestrator.requests_fleet("spawn as many parallel agents as possible")
    assert master_orchestrator.requests_fleet("run a fleet workflow")
    assert not master_orchestrator.requests_fleet("review this one file")


def test_run_delegated_agent_cap_is_configurable(monkeypatch):
    monkeypatch.setenv("SONDER_MAX_AGENTS", "24")

    result = master_orchestrator.run_delegated(
        "fan out",
        worker_fn=lambda prompt: "ok",
        audit_fn=lambda prompt: "merged",
        agents=99,
    )

    assert master_orchestrator.max_agents() == 24
    assert len(result["agents"]) == 24


GIB = 1024 ** 3


def _fake_hardware(
    monkeypatch, *, cpus=16, ram_avail_gib=10, vram_gib=0.0, model_gib=0.0,
    ollama_parallel=16,
):
    """Pin every hardware probe so capacity() tests never read the real host.

    vram_gib=0 means "no GPU detected" -> VRAM does not constrain.
    ollama_parallel defaults to the absolute max so it is never the binding
    constraint unless a test deliberately makes it one (the real machine's
    OLLAMA_NUM_PARALLEL must not leak in and skew unrelated assertions).
    """
    monkeypatch.delenv("SONDER_MAX_AGENTS", raising=False)
    monkeypatch.delenv("SONDER_PARALLEL_WORKERS", raising=False)
    monkeypatch.setattr(master_orchestrator.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(
        master_orchestrator, "physical_memory_bytes",
        lambda: (16 * GIB, int(ram_avail_gib * GIB)),
    )
    monkeypatch.setattr(
        master_orchestrator, "gpu_memory_bytes",
        lambda: (int(vram_gib * GIB), int(vram_gib * GIB)),
    )
    monkeypatch.setattr(
        master_orchestrator, "fleet_model_bytes", lambda: int(model_gib * GIB)
    )
    monkeypatch.setattr(
        master_orchestrator, "ollama_parallel_limit", lambda: int(ollama_parallel)
    )


def test_capacity_separates_agent_ceiling_from_memory_safe_worker_slots(monkeypatch):
    # No GPU -> RAM/CPU bound. Scarce RAM must throttle concurrency.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=2)
    low = master_orchestrator.capacity(32)

    assert low["agent_ceiling"] == 32
    assert low["requested_agents"] == 32
    assert low["source"] == "auto"
    # usable = 2.0 - 1.5 reserve = 0.5 GiB; at 0.25 GiB/worker -> 2 slots.
    assert low["worker_slots"] == 2
    assert low["bound_by"] == "ram"

    # Plentiful RAM -> the CPU/policy ceiling binds instead, not RAM.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10)
    healthy = master_orchestrator.capacity(32)
    assert healthy["worker_slots"] == 8
    assert healthy["bound_by"] in ("cpu", "policy")


def test_capacity_is_bound_by_vram_when_model_nearly_fills_the_card(monkeypatch):
    # The real RTX-4050 case: a 7B model (4.36 GiB) on a 6 GiB card leaves
    # ~1.1 GiB of headroom -> only ~2 concurrent KV caches fit.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, vram_gib=6.0, model_gib=4.36)

    report = master_orchestrator.capacity(32)

    assert report["worker_slots"] == 2
    assert report["bound_by"] == "gpu_vram"
    assert report["slot_limits"]["gpu_vram"] == 2
    # RAM/CPU had plenty of room -- VRAM is what actually limits it.
    assert report["slot_limits"]["cpu"] == 8
    assert report["slot_limits"]["ram"] > 8


def test_capacity_scales_up_on_a_big_card_and_down_on_a_small_one(monkeypatch):
    # Same model, roomy card -> VRAM stops being the binding constraint.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, vram_gib=24.0, model_gib=4.36)
    big = master_orchestrator.capacity(32)
    assert big["slot_limits"]["gpu_vram"] >= 8
    assert big["bound_by"] in ("cpu", "policy")

    # Same card, a small model -> far more parallel sequences fit.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, vram_gib=6.0, model_gib=0.92)
    small_model = master_orchestrator.capacity(32)
    assert small_model["slot_limits"]["gpu_vram"] > 2


def test_capacity_serializes_rather_than_thrashing_when_model_overcommits_vram(monkeypatch):
    # A 40 GiB model on a 24 GiB card cannot batch at all -- run one at a time
    # instead of spilling to CPU / thrashing.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, vram_gib=24.0, model_gib=40.0)

    report = master_orchestrator.capacity(32)

    assert report["worker_slots"] == 1
    assert report["bound_by"] == "gpu_vram"


def test_capacity_ignores_vram_when_no_gpu_is_detected(monkeypatch):
    # CPU-only inference: VRAM must not appear as a (zero) constraint.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, vram_gib=0.0, model_gib=4.36)

    report = master_orchestrator.capacity(32)

    assert "gpu_vram" not in report["slot_limits"]
    assert report["worker_slots"] == 8
    assert "not detected" in master_orchestrator.format_capacity(report)


def test_gpu_worker_slots_is_unconstrained_when_model_size_is_unknown(monkeypatch):
    # A GPU we can see but a model we can't size -> don't invent a limit.
    monkeypatch.setattr(
        master_orchestrator, "gpu_memory_bytes", lambda: (6 * GIB, 6 * GIB)
    )
    monkeypatch.setattr(master_orchestrator, "fleet_model_bytes", lambda: 0)

    assert master_orchestrator.gpu_worker_slots() == 0


def test_capacity_warns_when_ollama_would_serialize_the_slots(monkeypatch):
    # The real bug found 2026-07-13: OLLAMA_NUM_PARALLEL unset -> Ollama batches
    # one sequence at a time, so every worker slot past the first buys nothing
    # (measured: 2 concurrent requests took 2.07x a single one). Capacity must
    # say so out loud instead of advertising concurrency that does not exist.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, ollama_parallel=0)

    report = master_orchestrator.capacity(32)

    assert report["worker_slots"] > 1
    assert "OLLAMA_NUM_PARALLEL is unset" in report["warning"]
    assert "WARNING" in master_orchestrator.format_capacity(report)


def test_ollama_batch_width_caps_real_concurrency(monkeypatch):
    # Handing Ollama more concurrent requests than it will batch just queues
    # them, so its batch width is a hard ceiling on the slot count.
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=10, ollama_parallel=2)

    report = master_orchestrator.capacity(32)

    assert report["slot_limits"]["ollama_num_parallel"] == 2
    assert report["worker_slots"] == 2
    assert report["bound_by"] == "ollama_num_parallel"
    assert not report["warning"]  # matched to hardware -> nothing to warn about


def test_ollama_parallel_limit_reads_process_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "4")
    assert master_orchestrator.ollama_parallel_limit() == 4

    # Garbage is treated as unset rather than crashing capacity().
    monkeypatch.setenv("OLLAMA_NUM_PARALLEL", "not-a-number")
    assert master_orchestrator.ollama_parallel_limit() == 0


def test_parallel_worker_override_is_explicit_and_bounded(monkeypatch):
    _fake_hardware(monkeypatch, cpus=16, ram_avail_gib=2)
    monkeypatch.setenv("SONDER_PARALLEL_WORKERS", "6")

    report = master_orchestrator.capacity(10)

    assert report["worker_slots"] == 6
    assert report["source"] == "SONDER_PARALLEL_WORKERS"
    assert "concurrent worker slots: 6" in master_orchestrator.format_capacity(report)


def test_delegated_fleet_limits_actual_concurrency(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 2)
    lock = threading.Lock()
    current = {"active": 0, "maximum": 0}

    def worker(prompt):
        with lock:
            current["active"] += 1
            current["maximum"] = max(current["maximum"], current["active"])
        # Leave enough overlap for the process-shared SQLite start transition;
        # the assertion concerns model-call concurrency, not ledger connection time.
        time.sleep(0.12)
        with lock:
            current["active"] -= 1
        return "ok"

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=worker, audit_fn=lambda prompt: "merged", agents=6,
    )

    assert len(result["agents"]) == 6
    assert result["worker_slots"] == 2
    assert current["maximum"] == 2


def test_start_delegated_returns_before_background_workers_finish(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    started = threading.Event()
    release = threading.Event()

    def worker(prompt):
        started.set()
        assert release.wait(2)
        return "worker result"

    result = master_orchestrator.start_delegated(
        "background fleet",
        worker_fn=worker,
        audit_fn=lambda prompt: "audited result",
        agents=2,
    )

    assert result["background"] is True
    assert result["output"] == "RUNNING"
    assert len(result["agents"]) == 2
    assert started.wait(1)
    assert master_orchestrator.snapshot(include_finished=False)["active_agents"] > 0

    release.set()
    deadline = time.time() + 3
    while time.time() < deadline:
        snap = master_orchestrator.snapshot()
        if snap["active_agents"] == 0:
            break
        time.sleep(0.02)

    assert snap["active_agents"] == 0
    assert snap["latest_master_result"] == "audited result"


def test_cancel_master_skips_queued_workers_and_discards_running_result(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    started = threading.Event()
    release = threading.Event()
    calls = []
    audited = []
    result_box = {}

    def worker(prompt):
        calls.append(prompt)
        started.set()
        assert release.wait(2)
        return "late result"

    def run():
        result_box["result"] = master_orchestrator.run_delegated(
            "cancel fleet",
            worker_fn=worker,
            audit_fn=lambda prompt: audited.append(prompt) or "merged",
            agents=4,
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    snap = master_orchestrator.snapshot(include_finished=False, limit=20)
    master_id = next(row["id"] for row in snap["agents"] if row["role"] == "master")

    canceled = master_orchestrator.request_cancel(master_id)
    release.set()
    thread.join(3)

    assert not thread.is_alive()
    assert canceled["matched"] == 5
    assert canceled["queued"] == 3
    assert canceled["running"] == 2
    assert canceled["model_calls"] == 1
    assert result_box["result"]["output"] == "CANCELLED"
    assert len(calls) == 1
    assert audited == []
    final = master_orchestrator.snapshot(limit=20)
    assert final["active_agents"] == 0
    assert {row["status"] for row in final["agents"]} == {"cancelled"}


def test_cancelled_queued_worker_cannot_transition_to_running():
    calls = []
    agent_id = master_orchestrator._new_agent("agent", "queued work")
    master_orchestrator.request_cancel(agent_id)

    output = master_orchestrator._run_worker(
        agent_id, "prompt", lambda prompt: calls.append(prompt) or "unexpected",
    )

    assert output == "CANCELLED"
    assert calls == []
    row = master_orchestrator.snapshot(limit=5)["agents"][0]
    assert row["status"] == "cancelled"


def test_active_model_call_count_tracks_only_live_http_lanes():
    active = master_orchestrator._new_agent("agent", "active model")
    queued = master_orchestrator._new_agent("agent", "queued without model")
    assert master_orchestrator._start_agent(
        active, "calling model", in_model_call=True,
    )
    assert master_orchestrator._start_agent(
        queued, "local preparation", in_model_call=False,
    )

    assert master_orchestrator.active_model_call_count() == 1

    master_orchestrator.request_cancel("all")
    # Cancellation is cooperative: the HTTP lane remains owned until its
    # blocking model request returns and the worker finalizes.
    assert master_orchestrator.active_model_call_count() == 1
    master_orchestrator._finish(active, output="late result")
    assert master_orchestrator.active_model_call_count() == 0


def test_child_created_after_parent_cancellation_inherits_cancel_state():
    master_id = master_orchestrator._new_agent("master", "parent")
    master_orchestrator.request_cancel(master_id)

    child_id = master_orchestrator._new_agent(
        "agent", "late child", parent_id=master_id,
    )

    child = next(
        row for row in master_orchestrator.snapshot(limit=5)["agents"]
        if row["id"] == child_id
    )
    assert child["status"] == "cancelled"
    assert child["cancel_requested"] is True


def test_snapshot_active_count_is_not_clipped_by_display_limit():
    for index in range(25):
        master_orchestrator._new_agent("agent", "task %d" % index)

    snap = master_orchestrator.snapshot(include_finished=False, limit=5)

    assert snap["active_agents"] == 25
    assert snap["total_listed"] == 5


def test_hot_reload_preserves_owner_and_active_execution_state():
    owner_id = master_orchestrator._OWNER_ID
    agent_id = master_orchestrator._new_agent("agent", "survive reload")

    reloaded = importlib.reload(master_orchestrator)
    snap = reloaded.snapshot(include_finished=False, limit=5)

    assert reloaded._OWNER_ID == owner_id
    assert any(row["id"] == agent_id for row in snap["agents"])


def test_hot_reload_preserves_worker_failure_sentinel_identity():
    sentinel = master_orchestrator._WORKER_FAILED

    reloaded = importlib.reload(master_orchestrator)

    assert reloaded._WORKER_FAILED is sentinel


def test_inline_and_audit_bind_current_ledger_agent():
    inline_ids = []
    audit_ids = []

    inline = master_orchestrator.run_inline(
        "inline",
        lambda prompt: inline_ids.append(
            getattr(master_orchestrator._WORKER_LOCAL, "agent_id", None)
        ) or "done",
    )
    delegated = master_orchestrator.run_delegated(
        "delegate",
        worker_fn=lambda prompt: "worker",
        audit_fn=lambda prompt: audit_ids.append(
            getattr(master_orchestrator._WORKER_LOCAL, "agent_id", None)
        ) or "merged",
        agents=1,
    )

    assert inline_ids == [inline["master_id"]]
    assert audit_ids == [delegated["master_id"]]


def test_requires_repository_tools_detects_explicit_file_paths():
    # Regression (2026-07-13 audit): "generate a summary of the file <abs path>"
    # missed the repository lane (verb not read/inspect/...) and routed to an
    # ungrounded generation path that fabricated file contents. An absolute path
    # to a concrete source/text file names repository state regardless of verb.
    assert master_orchestrator.requires_repository_tools(
        r"generate a summary of the file D:\SparkEngine\Tests\TestFontSystem.cpp")
    assert master_orchestrator.requires_repository_tools(
        "summarize /home/u/app/main.py and list its classes")
    # Relative paths and greenfield tasks must NOT be pulled into the repo lane.
    assert not master_orchestrator.requires_repository_tools(
        "write a python script that saves output to results.txt")
    assert not master_orchestrator.requires_repository_tools(
        "Create a C++ 2.5D isometric RPG game with in-house assets")
    # Embedded evidence still short-circuits to False (answer from the excerpt).
    assert not master_orchestrator.requires_repository_tools(
        "summarize /home/u/app/main.py\n```\nclass A: pass\n```")
