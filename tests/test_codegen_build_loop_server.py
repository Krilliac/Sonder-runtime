"""What the codegen build loop believes about disk and about the compiler.

Both pins here are failures that were measured in the shipped tool, not
imagined: the loop read a key file_ops.read_file does not return (so every
guard that needs the current file silently no-opped), and it derived
BUILD SUCCEEDED from "no line matched the error regex" while throwing away
the build process's own exit status.
"""
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.bootstrap import strategy as strategy_bootstrap
from sonder_runtime.bootstrap.strategy import compose_strategy_trace
from sonder_runtime.domain.strategy.models import FailureClass
from sonder_runtime.platform.paths import state_path


def _build(ok=True, stdout="", stderr="", timed_out=False):
    """The exact dict shape workbench.run_program returns (single return path)."""
    return {
        "ok": ok,
        "program": "build",
        "command": "build",
        "cwd": ".",
        "returncode": 0 if ok else 1,
        "timed_out": timed_out,
        "elapsed_ms": 1,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": False,
        "stderr_truncated": False,
    }


def _prepare(monkeypatch, tmp_path, run_program):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    monkeypatch.setattr(server.workbench, "run_program", run_program)


def _enable_strategy(monkeypatch, home):
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_STRATEGY_OBSERVE", "1")
    monkeypatch.setattr(
        server, "_application",
        lambda: SimpleNamespace(
            unit_of_work=lambda: UnitOfWorkAdapter(str(home / "memory.db")),
        ),
    )


def _enable_codegen_canary(monkeypatch, tmp_path):
    _enable_strategy(monkeypatch, tmp_path / "home")
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "canary")
    monkeypatch.setenv("SONDER_STRATEGY_CANARY_PERCENT", "100")
    monkeypatch.setattr(server, "_ensemble_targets", lambda _tiers: ([
        ("code", "local-codegen-test"),
    ], []))
    monkeypatch.setattr(server, "_auto_model_context", lambda _model: 2048)
    # Exercise the canary host path with a test-only runner. Production has no
    # lower-privilege build authority and refuses this rollout before a build.
    monkeypatch.setattr(
        strategy_bootstrap, "compose_isolated_codegen_build",
        lambda: strategy_bootstrap.IsolatedCodegenBuild(server._codegen_build),
    )


def _project_guard(tmp_path):
    scope = "codegen-scope-" + hashlib.sha256(
        str(tmp_path.resolve()).encode(),
    ).hexdigest()
    return compose_strategy_trace().scope_guard(scope)


def _canary_call(tmp_path, files_json='{"main.c": "an entry point"}'):
    return server.codegen_build_loop(
        str(tmp_path), files_json, "build", tiers="code", attempts=2,
    )


def test_codegen_selected_canary_seals_one_attempt_and_releases_project(monkeypatch, tmp_path):
    fixed = "int main(void) { return 0; }"
    build_calls = []

    def build(*args, **kwargs):
        build_calls.append(1)
        source = tmp_path / "main.c"
        return _build(ok=source.exists() and source.read_text().strip() == fixed,
                      stdout="" if source.exists() and source.read_text().strip() == fixed
                      else "main.c:1: error: missing entry point")

    _prepare(monkeypatch, tmp_path, build)
    _enable_codegen_canary(monkeypatch, tmp_path)
    sends = []

    def generate(*args, **kwargs):
        guard = _project_guard(tmp_path)
        assert guard is not None
        member = guard["member_run_ids"][0]
        usage = compose_strategy_trace().pending(member)["usage"]
        assert usage["model_calls"] == 1
        assert usage["wall_seconds"] == 330
        assert 1 <= kwargs["timeout"] <= 330
        sends.append(1)
        return fixed

    monkeypatch.setattr(server, "_codegen_pinned_generation", generate)
    report = _canary_call(tmp_path)
    assert "BUILD SUCCEEDED" in report
    assert len(sends) == 1
    assert len(build_calls) == 3
    assert _project_guard(tmp_path) is None


def test_codegen_selected_canary_dispatches_critic_and_rotation_within_budget(monkeypatch, tmp_path):
    broken = "int main(void) { return missing; }"
    fixed = "int main(void) { return 0; }"

    def build(*args, **kwargs):
        source = tmp_path / "main.c"
        green = source.exists() and source.read_text().strip() == fixed
        return _build(ok=green, stdout="" if green else "main.c:1: error: missing name")

    _prepare(monkeypatch, tmp_path, build)
    _enable_codegen_canary(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda _tiers: ([("code", "coder"), ("reasoning", "critic")], []),
    )
    routes = []
    critiques = []

    def generate(prompt, *, model, **kwargs):
        routes.append(model)
        assert _project_guard(tmp_path) is not None
        return fixed if model == "critic" else broken

    def critique(prompt, **kwargs):
        critiques.append(kwargs["model"])
        assert kwargs["single_send"] is True
        return "Replace the missing identifier with a constant."

    monkeypatch.setattr(server, "_codegen_pinned_generation", generate)
    monkeypatch.setattr(server, "_codegen_critic_generation", critique)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=4,
    )
    assert "BUILD SUCCEEDED" in report
    assert routes == ["coder", "coder", "coder", "critic"]
    assert critiques == ["critic"]
    assert _project_guard(tmp_path) is None


def test_codegen_production_canary_requires_isolated_build_before_any_effect(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, lambda *a, **k: pytest.fail("build dispatched"))
    _enable_codegen_canary(monkeypatch, tmp_path)
    monkeypatch.setattr(strategy_bootstrap, "compose_isolated_codegen_build", lambda: None)
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: pytest.fail("model sent"))
    assert "isolated build authority unavailable" in _canary_call(tmp_path)


def test_codegen_missing_existing_checkpoint_db_blocks_even_with_rollout_off(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, lambda *a, **k: pytest.fail("build dispatched"))
    _enable_strategy(monkeypatch, tmp_path / "home")
    trace = compose_strategy_trace()
    trace.acquire_scope_guard(
        "scope-1", "owner-1", objective_digest="a" * 64,
        member_run_ids=("member-1",),
    )
    Path(state_path("strategy/checkpoints.db", "SONDER_STRATEGY_CHECKPOINT_DB")).unlink()
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "sealed strategy state unavailable" in _canary_call(tmp_path)


def test_codegen_repair_uses_bounded_durable_strategy_memory_before_model(monkeypatch, tmp_path):
    broken = "int main(void) { return missing; }"
    fixed = "int main(void) { return 0; }"

    def build(*args, **kwargs):
        source = tmp_path / "main.c"
        green = source.exists() and source.read_text().strip() == fixed
        return _build(ok=green, stdout="" if green else "main.c:1: error: missing")

    _prepare(monkeypatch, tmp_path, build)
    _enable_strategy(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(server, "_ensemble_targets", lambda _tiers: ([
        ("code", "local-codegen-test"),
    ], []))
    monkeypatch.setattr(server, "_auto_model_context", lambda _model: 8192)
    prompts = []

    def generate(prompt, **kwargs):
        prompts.append(prompt)
        return fixed if "past_strategy_observations" in prompt else broken

    monkeypatch.setattr(server, "ensemble_answer", generate)
    monkeypatch.setattr(server, "_codegen_observed_generation", generate)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code", attempts=2, num_predict=128,
    )
    assert "BUILD SUCCEEDED" in report
    assert len(prompts) == 2
    assert "past_strategy_observations" not in prompts[0]
    assert "past_strategy_observations" in prompts[1]
    assert "advisory_only" in prompts[1]
    assert broken in prompts[1] and "main.c:1: error: missing" in prompts[1]


def test_codegen_memory_selection_stays_unattributed_without_model_response(monkeypatch, tmp_path):
    _prepare(monkeypatch, tmp_path, lambda *a, **k: _build(
        ok=False, stdout="main.c:1: error: missing",
    ))
    home = tmp_path / "home"
    _enable_strategy(monkeypatch, home)
    monkeypatch.setattr(server, "_ensemble_targets", lambda _tiers: ([
        ("code", "local-codegen-test"),
    ], []))
    monkeypatch.setattr(server, "_auto_model_context", lambda _model: 8192)
    monkeypatch.setattr(server, "ensemble_answer", lambda *a, **k: "int x = missing;")
    def fail_after_selection(prompt, **kwargs):
        assert "past_strategy_observations" in prompt
        raise server.ModelCallError("transport", "provider unavailable")

    monkeypatch.setattr(server, "_codegen_observed_generation", fail_after_selection)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code", attempts=2, num_predict=128,
    )
    assert "provider unavailable" in report
    with sqlite3.connect(home / "memory.db") as connection:
        rows = connection.execute(
            "SELECT outcome FROM strategy_memory_selection",
        ).fetchall()
    assert rows == [(None,)]


def test_codegen_model_failure_blocks_alias_and_rollout_off_before_build(monkeypatch, tmp_path):
    build_calls = []
    _prepare(monkeypatch, tmp_path, lambda *a, **k: (
        build_calls.append(1) or _build(ok=False, stdout="main.c:1: error: broken")
    ))
    _enable_codegen_canary(monkeypatch, tmp_path)
    sends = []

    def failed_send(*args, **kwargs):
        sends.append(1)
        raise server.ModelCallError("transport", "lost after dispatch")

    monkeypatch.setattr(server, "_codegen_pinned_generation", failed_send)
    assert "strategy canary paused" in _canary_call(tmp_path)
    assert len(sends) == 1
    assert len(build_calls) == 1
    assert _project_guard(tmp_path) is not None
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    alias = str(tmp_path / ".")
    report = server.codegen_build_loop(
        alias, '{"other.c": "another file"}', "build", tiers="code",
    )
    assert "prior project effect" in report
    assert len(sends) == 1
    assert len(build_calls) == 1


def test_codegen_candidate_build_crash_keeps_pending_and_project_guard(monkeypatch, tmp_path):
    calls = []

    def build(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt("crash after model before verifier")
        return _build(ok=False, stdout="main.c:1: error: broken")

    _prepare(monkeypatch, tmp_path, build)
    _enable_codegen_canary(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: "int x = 1;")
    with pytest.raises(KeyboardInterrupt):
        _canary_call(tmp_path)
    guard = _project_guard(tmp_path)
    assert guard is not None
    assert compose_strategy_trace().pending(guard["member_run_ids"][0]) is not None
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "prior project effect" in _canary_call(tmp_path)
    assert len(calls) == 2


def test_codegen_final_build_timeout_keeps_project_guard(monkeypatch, tmp_path):
    fixed = "int main(void) { return 0; }"
    calls = []

    def build(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            return _build(ok=False, timed_out=True)
        return _build(ok=len(calls) == 2,
                      stdout="" if len(calls) == 2 else "main.c:1: error: broken")

    _prepare(monkeypatch, tmp_path, build)
    _enable_codegen_canary(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: fixed)
    assert "build outcome needs host inspection" in _canary_call(tmp_path)
    assert _project_guard(tmp_path) is not None
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "prior project effect" in _canary_call(tmp_path)
    assert len(calls) == 3


def test_codegen_baseline_timeout_blocks_model_and_keeps_guard(monkeypatch, tmp_path):
    calls = []
    _prepare(monkeypatch, tmp_path, lambda *a, **k: (
        calls.append(1) or _build(ok=False, timed_out=True)
    ))
    _enable_codegen_canary(monkeypatch, tmp_path)
    sends = []
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: sends.append(1))
    assert "build outcome needs host inspection" in _canary_call(tmp_path)
    assert _project_guard(tmp_path) is not None
    assert sends == []
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "prior project effect" in _canary_call(tmp_path)
    assert len(calls) == 1


def test_codegen_reservation_failure_blocks_model_and_reinvocation(monkeypatch, tmp_path):
    calls = []
    _prepare(monkeypatch, tmp_path, lambda *a, **k: (
        calls.append(1) or _build(ok=False, stdout="main.c:1: error: broken")
    ))
    _enable_codegen_canary(monkeypatch, tmp_path)
    sends = []
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: sends.append(1))
    monkeypatch.setattr(StrategyTraceService, "reserve_next", lambda *a, **k: (
        _ for _ in ()).throw(OSError("save failed"))
    )
    assert "could not be reserved" in _canary_call(tmp_path)
    assert sends == []
    assert _project_guard(tmp_path) is not None
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "prior project effect" in _canary_call(tmp_path)
    assert len(calls) == 1


def test_codegen_mutated_write_then_error_blocks_next_invocation(monkeypatch, tmp_path):
    calls = []
    _prepare(monkeypatch, tmp_path, lambda *a, **k: (
        calls.append(1) or _build(ok=False, stdout="main.c:1: error: broken")
    ))
    _enable_codegen_canary(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: "int x = 1;")
    original_write = server.file_ops.write_file

    def uncertain_write(*args, **kwargs):
        original_write(*args, **kwargs)
        raise OSError("ack lost after mutation")

    monkeypatch.setattr(server.file_ops, "write_file", uncertain_write)
    assert "could not write" in _canary_call(tmp_path)
    guard = _project_guard(tmp_path)
    assert guard is not None
    assert compose_strategy_trace().pending(guard["member_run_ids"][0]) is None
    assert (tmp_path / "main.c").read_text().strip() == "int x = 1;"
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "prior project effect" in _canary_call(tmp_path)
    assert len(calls) == 1


def test_codegen_failed_attempt_completion_seal_keeps_pending(monkeypatch, tmp_path):
    calls = []
    _prepare(monkeypatch, tmp_path, lambda *a, **k: (
        calls.append(1) or _build(ok=False, stdout="main.c:1: error: broken")
    ))
    _enable_codegen_canary(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_codegen_pinned_generation", lambda *a, **k: "int x = 1;")
    monkeypatch.setattr(StrategyTraceService, "record_reserved", lambda *a, **k: (
        _ for _ in ()).throw(OSError("checkpoint save failed"))
    )
    assert "result could not be sealed" in _canary_call(tmp_path)
    guard = _project_guard(tmp_path)
    assert guard is not None
    assert compose_strategy_trace().pending(guard["member_run_ids"][0]) is not None
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "off")
    assert "prior project effect" in _canary_call(tmp_path)
    assert len(calls) == 2


def test_second_attempt_repairs_from_bounded_compiler_feedback(monkeypatch, tmp_path):
    diagnostic = "main.c:1: error: unknown type name 'widget'"
    broken = "int main(void) { return widget; }"
    fixed = "int main(void) { return 0; }"

    def build(*args, **kwargs):
        source = tmp_path / "main.c"
        if source.exists() and source.read_text(encoding="utf-8").strip() == fixed:
            return _build(ok=True, stdout="build complete")
        return _build(ok=False, stdout=diagnostic)

    _prepare(monkeypatch, tmp_path, build)
    prompts = []

    def generate(prompt, **kwargs):
        prompts.append(prompt)
        return fixed if diagnostic in prompt and broken in prompt else broken

    monkeypatch.setattr(server, "ensemble_answer", generate)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build", attempts=4,
    )

    assert len(prompts) == 2
    assert diagnostic in prompts[1]
    assert broken in prompts[1]
    assert "BUILD SUCCEEDED" in report
    assert (tmp_path / "main.c").read_text(encoding="utf-8").strip() == fixed


def test_observed_codegen_attempts_restore_without_source_or_diagnostics(monkeypatch, tmp_path):
    diagnostic = "main.c:1: error: unknown type name 'private_widget'"
    broken = "int main(void) { return private_widget; }"
    fixed = "int main(void) { return 0; }"

    def build(*args, **kwargs):
        source = tmp_path / "main.c"
        if source.exists() and source.read_text(encoding="utf-8").strip() == fixed:
            return _build(ok=True, stdout="build complete")
        return _build(ok=False, stdout=diagnostic)

    _prepare(monkeypatch, tmp_path, build)
    home = tmp_path / "home"
    _enable_strategy(monkeypatch, home)
    monkeypatch.setattr(
        server, "ensemble_answer",
        lambda prompt, **kwargs: fixed if diagnostic in prompt and broken in prompt else broken,
    )

    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build", attempts=4,
    )

    assert "BUILD SUCCEEDED" in report
    database = home / "strategy" / "checkpoints.db"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT run_id, payload_json FROM runtime_checkpoint ORDER BY generation"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[0] == rows[0][0] for row in rows)
    assert all(diagnostic not in row[1] and broken not in row[1] for row in rows)

    restored = compose_strategy_trace(
        db_path=database, key_path=home / "strategy-private" / "checkpoint.key",
    ).history(rows[0][0])
    assert [attempt.outcome for attempt in restored] == ["failed", "succeeded"]
    assert [attempt.usage.attempts for attempt in restored] == [1, 1]
    assert restored[0].failure.classification is FailureClass.BUILD_FAILURE
    assert restored[0].progress_after.metrics[0].value == 1
    assert restored[1].progress_after.metrics[0].value == 0
    assert restored[0].progress_after.scope_digest == restored[1].progress_before.scope_digest
    with sqlite3.connect(home / "memory.db") as connection:
        indexed = connection.execute(
            "SELECT outcome, project_digest FROM strategy_experience ORDER BY rowid"
        ).fetchall()
    assert [outcome for outcome, _digest in indexed] == ["failed", "succeeded"]
    assert all(str(tmp_path) not in digest for _outcome, digest in indexed)


def test_explicit_distinct_tiers_enable_scoped_critic_then_rotation(monkeypatch, tmp_path):
    diagnostic = "main.c:1: error: unknown type name 'widget'"
    broken = "int main(void) { return widget; }"
    fixed = "int main(void) { return 0; }"
    def build(*args, **kwargs):
        path = tmp_path / "main.c"
        green = path.exists() and path.read_text(encoding="utf-8").strip() == fixed
        return _build(ok=green, stdout="build complete" if green else diagnostic)

    _prepare(monkeypatch, tmp_path, build)
    home = tmp_path / "home"
    _enable_strategy(monkeypatch, home)
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda tiers: ([("code", "coder"), ("reasoning", "critic")], []),
    )
    calls = []
    critic = []

    def diagnose(prompt, **options):
        critic.append(prompt)
        assert options["tier"] == "reasoning" and options["model"] == "critic"
        return "The widget name is undefined; return 0 instead."

    def answer(prompt, **options):
        calls.append((prompt, options))
        if options["tiers"] == "reasoning":
            return fixed
        return broken

    monkeypatch.setattr(server, "_codegen_critic_generation", diagnose)
    monkeypatch.setattr(server, "ensemble_answer", answer)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=4,
    )
    generation = [opts["tiers"] for _, opts in calls if opts["mode"] == "code"]
    assert generation == ["code", "code", "code", "reasoning"]
    assert len(critic) == 1
    assert "an entry point" in critic[0]
    assert diagnostic in critic[0] and broken in critic[0]
    assert "The widget name is undefined" in calls[2][0]
    assert "BUILD SUCCEEDED" in report
    with sqlite3.connect(home / "strategy" / "checkpoints.db") as connection:
        run_id = connection.execute(
            "SELECT run_id FROM runtime_checkpoint LIMIT 1"
        ).fetchone()[0]
    history = compose_strategy_trace(
        db_path=home / "strategy" / "checkpoints.db",
        key_path=home / "strategy-private" / "checkpoint.key",
    ).history(run_id)
    assert [item.usage.model_calls for item in history] == [1, 1, 2, 1]
    assert [item.usage.critic_calls for item in history] == [0, 0, 1, 0]
    assert [item.usage.strategy_switches for item in history] == [0, 0, 0, 1]


def test_same_model_alias_keeps_no_progress_stop_without_critic(monkeypatch, tmp_path):
    diagnostic = "main.c:1: error: unknown type name 'widget'"
    _prepare(monkeypatch, tmp_path, lambda *args, **kwargs: _build(ok=False, stdout=diagnostic))
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda tiers: ([("code", "same-model")], []),
    )
    calls = []

    def answer(prompt, **options):
        calls.append(options)
        return "int main(void) { return widget; }"

    monkeypatch.setattr(server, "ensemble_answer", answer)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=4,
    )
    assert len(calls) == 2
    assert all(options["mode"] == "code" for options in calls)
    assert "no progress" in report


def test_failed_critic_degrades_to_feedback_and_policy_still_bounds_rotation(
    monkeypatch, tmp_path,
):
    diagnostic = "main.c:1: error: unknown type name 'widget'"
    broken = "int main(void) { return widget; }"
    fixed = "int main(void) { return 0; }"

    def build(*args, **kwargs):
        path = tmp_path / "main.c"
        green = path.exists() and path.read_text(encoding="utf-8").strip() == fixed
        return _build(ok=green, stdout="ok" if green else diagnostic)

    _prepare(monkeypatch, tmp_path, build)
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda tiers: ([("code", "coder"), ("reasoning", "critic")], []),
    )
    calls = []

    def answer(prompt, **options):
        calls.append((prompt, options))
        return fixed if options["tiers"] == "reasoning" else broken

    def unavailable(prompt, **options):
        raise RuntimeError("critic unavailable")

    monkeypatch.setattr(server, "_codegen_critic_generation", unavailable)
    monkeypatch.setattr(server, "ensemble_answer", answer)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=4,
    )
    assert [opts["tiers"] for _, opts in calls if opts["mode"] == "code"] == [
        "code", "code", "code", "reasoning",
    ]
    assert "critic unavailable" in report
    assert "Independent critic suggestion" not in calls[2][0]
    assert "BUILD SUCCEEDED" in report


def test_incomplete_build_evidence_does_not_trigger_critic(monkeypatch, tmp_path):
    _prepare(
        monkeypatch, tmp_path,
        lambda *args, **kwargs: {
            **_build(ok=False, stdout="main.c:1: error: visible part"),
            "stdout_truncated": True,
        },
    )
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda tiers: ([("code", "coder"), ("reasoning", "critic")], []),
    )
    calls = []
    monkeypatch.setattr(
        server, "_codegen_critic_generation",
        lambda *args, **kwargs: pytest.fail("critic must not run"),
    )

    def answer(prompt, **options):
        calls.append(options)
        return "int main(void) { return widget; }"

    monkeypatch.setattr(server, "ensemble_answer", answer)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=4,
    )
    assert len(calls) == 4
    assert all(item["mode"] == "code" and item["tiers"] == "code" for item in calls)
    assert "BUILD SUCCEEDED" not in report


def test_staged_repair_still_stops_after_distinct_critic_and_rotation_fail(
    monkeypatch, tmp_path,
):
    diagnostic = "main.c:1: error: unknown type name 'widget'"
    _prepare(monkeypatch, tmp_path, lambda *args, **kwargs: _build(ok=False, stdout=diagnostic))
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda tiers: ([("code", "coder"), ("reasoning", "critic")], []),
    )
    calls = []
    critic = []

    def diagnose(prompt, **options):
        critic.append(prompt)
        return "Try a different type"

    def answer(prompt, **options):
        calls.append(options)
        return "int main(void) { return widget; }"

    monkeypatch.setattr(server, "_codegen_critic_generation", diagnose)
    monkeypatch.setattr(server, "ensemble_answer", answer)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=6,
    )
    assert [item["tiers"] for item in calls if item["mode"] == "code"] == [
        "code", "code", "code", "reasoning",
    ]
    assert len(critic) == 1
    assert "no progress" in report


def test_repair_restores_best_candidate_after_worse_followup(monkeypatch, tmp_path):
    better = "int main(void) { return widget; }"
    worse = "int main(void) { return nonsense; }"

    def build(*args, **kwargs):
        path = tmp_path / "main.c"
        if path.exists() and path.read_text(encoding="utf-8").strip() == worse:
            return _build(ok=False, stdout="main.c:1: error: x\nmain.c:2: error: y")
        return _build(ok=False, stdout="main.c:1: error: x")

    _prepare(monkeypatch, tmp_path, build)
    candidates = iter((better, worse))
    monkeypatch.setattr(server, "ensemble_answer", lambda *args, **kwargs: next(candidates))
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build", attempts=2,
    )
    assert "BUILD FAILED" in report
    assert (tmp_path / "main.c").read_text(encoding="utf-8").strip() == better


def test_staged_repair_stops_after_rotation_regresses_without_stall_fingerprint(
    monkeypatch, tmp_path,
):
    builds = []

    def build(*args, **kwargs):
        builds.append(1)
        n = len(builds)
        return _build(ok=False, stdout=f"main.c:{n}: error: changing failure {n}")

    _prepare(monkeypatch, tmp_path, build)
    monkeypatch.setattr(
        server, "_ensemble_targets",
        lambda tiers: ([("code", "coder"), ("reasoning", "critic")], []),
    )
    calls = []
    critic = []

    def diagnose(prompt, **options):
        critic.append(prompt)
        return "Try changing the declaration"

    def answer(prompt, **options):
        calls.append(options)
        return "int main(void) { return widget; }"

    monkeypatch.setattr(server, "_codegen_critic_generation", diagnose)
    monkeypatch.setattr(server, "ensemble_answer", answer)
    report = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
        tiers="code,reasoning", attempts=6,
    )
    assert len([item for item in calls if item["mode"] == "code"]) == 4
    assert len(critic) == 1
    assert "no measured build improvement" in report


def test_an_existing_clean_file_is_read_from_disk_and_not_regenerated(
    monkeypatch, tmp_path,
):
    """read() pulled data["content"], but read_file returns "text".

    `existing` was therefore always "", which disabled the shrink floor, the
    already-clean skip, the score-against-the-incumbent rule and the sibling
    API brief all at once.
    """
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _prepare(monkeypatch, tmp_path, lambda *a, **k: _build(ok=True, stdout="ok"))

    asked = []

    def fake_ensemble(prompt, **kwargs):
        asked.append(prompt)
        return "int main(void) { return 1; }"

    monkeypatch.setattr(server, "ensemble_answer", fake_ensemble)

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.c": "an entry point"}', "build",
    )

    assert "already clean, not regenerated" in out
    assert asked == []
    # The file the caller already had must survive untouched.
    assert (tmp_path / "main.c").read_text(encoding="utf-8") == (
        "int main(void) { return 0; }\n"
    )


def test_a_failing_exit_status_is_not_reported_as_a_green_build(
    monkeypatch, tmp_path,
):
    """A restore failure under a stricter regex exited 1 and read as SUCCESS."""
    (tmp_path / "main.cs").write_text("class C { }\n", encoding="utf-8")
    _prepare(
        monkeypatch, tmp_path,
        lambda *a, **k: _build(
            ok=False, stdout="error NU1101: Unable to find package Foo",
        ),
    )
    monkeypatch.setattr(server, "ensemble_answer", lambda prompt, **kw: "class C { }")

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.cs": "a class"}', "dotnet",
        build_args_json='["build"]', attempts=1, error_regex=r"CS\d{4}",
    )

    assert "BUILD SUCCEEDED" not in out
    assert "failure status" in out


def test_a_truncated_build_is_not_a_green_build_under_a_stricter_error_regex(
    monkeypatch, tmp_path,
):
    """The whole loop, driven through the documented `error_regex` knob.

    workbench dropped the tail of the build output, which is where the errors
    are. Under `CS\\d{4}` the harness's truncation notice does not match, so the
    parsed error list is EMPTY -- and an empty list from a build nobody
    finished reading is indistinguishable from a clean compile. The loop then
    skipped the file as "already clean" and reported BUILD SUCCEEDED.
    """
    (tmp_path / "main.cs").write_text("class C { }\n", encoding="utf-8")
    truncated = _build(ok=True, stdout="Build started 12:00:01")
    truncated["stdout_truncated"] = True
    _prepare(monkeypatch, tmp_path, lambda *a, **k: truncated)
    monkeypatch.setattr(server, "ensemble_answer", lambda prompt, **kw: "class C { }")

    out = server.codegen_build_loop(
        str(tmp_path), '{"main.cs": "a class"}', "dotnet",
        build_args_json='["build"]', attempts=1, error_regex=r"CS\d{4}",
    )

    assert "BUILD SUCCEEDED" not in out
    assert "MEASUREMENT INCOMPLETE" in out
    assert "already clean" not in out


def test_program_search_says_when_the_list_was_cut(monkeypatch):
    """The handler dropped workbench's truncated flag, so a PATH-order slice
    read as the machine's whole program list -- "absent" meant "not installed"."""
    activity_tracker.reset_for_tests()
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.workbench, "program_search",
        lambda query, **kw: {
            "query": query,
            "results": [{"name": "cl.exe", "path": "C:/cl.exe", "source": "PATH"}],
            "truncated": True,
        },
    )

    out = server.program_search(query="*", max_results=1)

    assert "cl.exe" in out
    assert "truncated" in out


def test_program_search_stays_quiet_when_nothing_was_cut(monkeypatch):
    activity_tracker.reset_for_tests()
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.workbench, "program_search",
        lambda query, **kw: {
            "query": query,
            "results": [{"name": "cl.exe", "path": "C:/cl.exe", "source": "PATH"}],
            "truncated": False,
        },
    )

    assert "truncated" not in server.program_search(query="cl")


def test_an_unreadable_backlog_is_reported_as_unknown_not_as_the_batch(
    monkeypatch,
):
    """`backlog = deferred` reinstated the floor-as-total bug it was fixing:
    when the count query lost the race with the campaign's own writers, the
    batch number was printed as the backlog (500 outstanding read as 0)."""
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 0,
    )
    monkeypatch.setattr(
        server.memory_store, "list_retryable_distillations",
        lambda conn, limit: [("iid-1", "compiled")],
    )
    # Signature-agnostic: this test asserts how the BACKLOG is reported and
    # makes no claim about the recorder's parameters. Pinned to `(iid, signal)`
    # it broke when #62 added required provenance -- and broke invisibly: the
    # drain swallows per-item exceptions, so the TypeError surfaced only as
    # `stored == 0`, which is the floor-reported-as-total shape this very test
    # exists to catch, arriving from the test's own double.
    monkeypatch.setattr(
        server, "_record_outcome_and_maybe_distill",
        lambda *a, **k: {"lesson_id": "lesson-1"},
    )

    def locked(conn):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        server.memory_store, "count_retryable_distillations", locked,
    )

    drain = server._drain_deferred_distillations(limit=1)

    assert drain["stored"] == 1
    assert drain["deferred"] == 0
    assert drain["backlog"] is None
    assert "unknown" in server._drain_backlog_text(drain)


class _DummyConn:
    """A connection stand-in. `list_retryable_distillations` and
    `count_retryable_distillations` are doubled, and `_stored_outcome_source`
    swallows its own errors, so nothing here reaches a real database -- which
    also keeps these tests off the operator's live store."""

    def close(self):
        pass


def _drain_fixture(monkeypatch, pending, recorder, backlog=0):
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 0,
    )
    monkeypatch.setattr(server, "_open_db", lambda *a, **k: _DummyConn())
    monkeypatch.setattr(
        server.memory_store, "list_retryable_distillations",
        lambda *a, **k: list(pending),
    )
    monkeypatch.setattr(
        server.memory_store, "count_retryable_distillations",
        lambda *a, **k: backlog,
    )
    monkeypatch.setattr(server, "_record_outcome_and_maybe_distill", recorder)


def test_an_item_that_raised_is_reported_as_failed_not_as_a_clean_zero(
    monkeypatch,
):
    """`except Exception: continue` made every per-item failure invisible.

    A batch whose every item raised returned `stored: 0, deferred: 0` -- byte
    for byte what a batch of genuinely-deferred-nothing looks like -- so the
    campaign line printed "lessons stored 0, still deferred in batch 0" and
    nothing anywhere said the recorder had not run. That is how the
    over-narrow double in the test above hid its own TypeError as `stored == 0`:
    the count is a floor and was reported as a total.
    """
    def raising(*args, **kwargs):
        raise TypeError("missing a required argument: 'source'")

    _drain_fixture(
        monkeypatch,
        [("iid-1", "compiled"), ("iid-2", "tests_passed")],
        raising,
    )

    drain = server._drain_deferred_distillations(limit=8)

    assert drain["drained"] == 2
    assert drain["stored"] == 0
    assert drain["deferred"] == 0
    assert drain["failed"] == 2


def test_every_drained_item_lands_in_exactly_one_bucket(monkeypatch):
    """The buckets must sum to the batch, or a count is a floor again.

    A mixed batch covering all five ways an item can end: stored, deferred,
    raised, a signal outside the vocabulary, and -- the one easiest to forget
    -- a recorder that returned normally while claiming neither a lesson nor a
    deferral. Nothing may fall between the buckets.
    """
    def recorder(interaction_id, signal, **kwargs):
        if interaction_id == "iid-store":
            return {"lesson_id": "lesson-1"}
        if interaction_id == "iid-defer":
            return {"distillation_deferred": True}
        if interaction_id == "iid-nothing":
            return {}
        raise RuntimeError("recorder exploded")

    _drain_fixture(
        monkeypatch,
        [
            ("iid-store", "compiled"),
            ("iid-defer", "compiled"),
            ("iid-raise", "compiled"),
            ("iid-bogus", "not-a-real-signal"),
            ("iid-nothing", "compiled"),
        ],
        recorder,
    )

    drain = server._drain_deferred_distillations(limit=8)

    assert drain["drained"] == 5
    assert drain["stored"] == 1
    assert drain["deferred"] == 1
    assert drain["failed"] == 1
    # Both the unknown signal and the recorder that claimed nothing.
    assert drain["skipped"] == 2
    accounted = (
        drain["stored"] + drain["deferred"] + drain["failed"] + drain["skipped"]
    )
    assert accounted == drain["drained"], drain


def test_the_campaign_line_says_when_items_failed(monkeypatch):
    """An unattended nightly run only sees these lines. A drain whose every
    item raised must not render as a quiet success."""
    def raising(*args, **kwargs):
        raise TypeError("missing a required argument: 'source'")

    _drain_fixture(monkeypatch, [("iid-1", "compiled")], raising)

    drain = server._drain_deferred_distillations(limit=8)

    assert "failed 1" in server._drain_summary_text(drain)


def test_a_clean_drain_still_renders_without_a_failure_clause(monkeypatch):
    """The healthy line stays byte-identical to what it has always been."""
    _drain_fixture(
        monkeypatch,
        [("iid-1", "compiled")],
        lambda *a, **k: {"lesson_id": "lesson-1"},
        backlog=3,
    )

    drain = server._drain_deferred_distillations(limit=8)

    assert drain["failed"] == 0
    assert server._drain_summary_text(drain) == (
        "deferred distillations drained: 1 (lessons stored 1, still deferred "
        "in batch 0, backlog remaining 3)"
    )
