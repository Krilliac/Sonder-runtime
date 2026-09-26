"""Issue #510 section 6/7: batching/coalescing guard and its canaries.

Unit tests pin the pure guard (``sonder_runtime.domain.batch_coalescing``).
The canaries drive the real ``server._agent_impl`` loop with a scripted model
and a recording dispatcher until the guard fires, and assert the refused
single call was never dispatched.  Negative controls prove ordinary traffic
(other families, mutating tools, below threshold, a new turn, a state change,
an unavailable batch tool) is left alone.
"""
from __future__ import annotations

import json

import pytest

import server
from sonder_runtime.domain import batch_coalescing as bc


READ = bc.BatchCounterpart("file_read", "context_pack", "path", "paths_json", "file-read")
DIGEST = bc.BatchCounterpart("file_digest", "digest_many", "path", "paths", "digest")


# -- pure guard -------------------------------------------------------------

def _read(guard, path, ok=True):
    refusal = guard.before_dispatch("file_read", {"path": path})
    if refusal is not None:
        return refusal
    return guard.after_dispatch("file_read", {"path": path}, ok=ok)


def test_resolve_counterparts_requires_registered_read_only_pairs():
    mutating = bc.BatchCounterpart("file_write", "file_batch_write", "path", "operations", "write")
    candidates = (READ, mutating, DIGEST)
    registered = {"file_read", "context_pack", "file_write", "file_batch_write", "file_digest"}

    active = bc.resolve_counterparts(
        candidates,
        registered=registered,
        # Even a mis-listed mutating pair is excluded by the state-changing set.
        read_only={"file_read", "context_pack", "file_write", "file_batch_write", "file_digest", "digest_many"},
        state_changing={"file_write", "file_batch_write"},
    )

    # digest_many is not registered, the write pair is mutating.
    assert active == (READ,)


def test_resolve_counterparts_rejects_unclassified_batch_tool():
    assert bc.resolve_counterparts(
        (READ,), registered={"file_read", "context_pack"},
        read_only={"file_read"}, state_changing=(),
    ) == ()


def test_declared_candidates_resolve_against_the_real_dispatcher():
    server._agent_batch_counterparts.cache_clear()
    try:
        active = server._agent_batch_counterparts()
    finally:
        server._agent_batch_counterparts.cache_clear()

    assert [(cp.single_tool, cp.batch_tool) for cp in active] == [("file_read", "context_pack")]
    for counterpart in active:
        for tool in (counterpart.single_tool, counterpart.batch_tool):
            assert tool in server.REPOSITORY_READ_ONLY_TOOLS
            assert tool not in server._WORK_MUTATION_TOOLS
            assert tool not in server._AGENT_EXECUTION_STATE_INVALIDATION_TOOLS


def test_unregistered_batch_tool_leaves_the_guard_inert(monkeypatch):
    server._agent_batch_counterparts.cache_clear()
    monkeypatch.setattr(
        server.tool_capabilities, "dispatch_names",
        lambda _dispatch: frozenset({"file_read", "text_search"}),
    )
    try:
        assert server._agent_batch_counterparts() == ()
    finally:
        server._agent_batch_counterparts.cache_clear()


def test_config_defaults_and_environment_parsing():
    assert bc.BatchCoalescingConfig() == bc.BatchCoalescingConfig(3, 6, 3)
    assert bc.BatchCoalescingConfig.from_environ({}) == bc.BatchCoalescingConfig()
    parsed = bc.BatchCoalescingConfig.from_environ({
        bc.ENV_ADVISORY_AFTER: "2", bc.ENV_REFUSE_AFTER: " 4 ", bc.ENV_MAX_REFUSALS: "1",
    })
    assert parsed == bc.BatchCoalescingConfig(2, 4, 1)


@pytest.mark.parametrize("env", [
    {bc.ENV_ADVISORY_AFTER: "1"},
    {bc.ENV_ADVISORY_AFTER: "5", bc.ENV_REFUSE_AFTER: "4"},
    {bc.ENV_REFUSE_AFTER: "21"},
    # 20 distinct single reads plus one more call cannot fit a 20-step turn,
    # so these values would silently switch a stage off.
    {bc.ENV_REFUSE_AFTER: "20"},
    {bc.ENV_ADVISORY_AFTER: "20", bc.ENV_REFUSE_AFTER: "20"},
    {bc.ENV_MAX_REFUSALS: "0"},
    {bc.ENV_MAX_REFUSALS: "11"},
    {bc.ENV_REFUSE_AFTER: "-3"},
    {bc.ENV_REFUSE_AFTER: "six"},
    {bc.ENV_REFUSE_AFTER: "٤"},
])
def test_config_rejects_out_of_range_or_malformed_values(env):
    with pytest.raises(ValueError):
        bc.BatchCoalescingConfig.from_environ(env)


def test_config_rejects_boolean_thresholds():
    with pytest.raises(ValueError):
        bc.BatchCoalescingConfig(advisory_after=True)


def test_invalid_operator_setting_keeps_guard_on_with_defaults():
    config = server._agent_batch_coalescing_config({bc.ENV_REFUSE_AFTER: "999"})
    assert config == bc.BatchCoalescingConfig()


def test_advisory_then_refusal_then_exhaustion():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(3, 4, 2))

    assert _read(guard, "a.py") is None
    assert _read(guard, "b.py") is None
    advisory = _read(guard, "c.py")
    assert isinstance(advisory, bc.BatchAdvisory)
    assert advisory.distinct_targets == 3
    assert "context_pack" in advisory.render()
    assert "The result above is unchanged" in advisory.render()
    assert isinstance(_read(guard, "d.py"), bc.BatchAdvisory)

    refusal = _read(guard, "e.py")
    assert isinstance(refusal, bc.BatchRefusal)
    assert not refusal.exhausted
    text = refusal.render()
    assert text.startswith("ERROR: HOST BATCH GUARD (batch_coalescing): file_read was not run.")
    assert 'context_pack {"paths_json": ["e.py"]}' in text
    assert refusal.telemetry()["action"] == "refusal"

    second = _read(guard, "f.py")
    assert isinstance(second, bc.BatchRefusal) and second.exhausted
    assert second.telemetry()["action"] == "exhausted"
    assert guard.snapshot()["refusals"] == 2


def test_rereading_a_counted_target_is_never_counted_or_refused():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 1))
    _read(guard, "src/a.py")
    _read(guard, "src/b.py")
    # Spelling variants of an already-read target are the same target.
    for spelling in ("src/a.py", "src\\a.py", "./src/./a.py", "SRC/A.py"):
        assert guard.before_dispatch("file_read", {"path": spelling}) is None
        assert guard.after_dispatch("file_read", {"path": spelling}, ok=True) is None
    assert isinstance(guard.before_dispatch("file_read", {"path": "src/c.py"}), bc.BatchRefusal)


def test_failed_and_malformed_single_calls_are_not_counted():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 1))
    for index in range(5):
        assert _read(guard, "missing%d.py" % index, ok=False) is None
    for malformed in ({}, {"path": ""}, {"path": 7}, None):
        assert guard.before_dispatch("file_read", malformed) is None
        assert guard.after_dispatch("file_read", malformed, ok=True) is None
    assert guard.snapshot()["families"] == {}


def test_successful_batch_call_and_reset_start_a_new_window():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 3))
    _read(guard, "a.py")
    _read(guard, "b.py")
    assert guard.after_dispatch("context_pack", {"paths_json": ["c.py"]}, ok=True) is None
    assert _read(guard, "c.py") is None

    _read(guard, "d.py")
    guard.reset()
    assert _read(guard, "e.py") is None
    assert guard.snapshot()["resets"] == 1


def test_distinct_families_keep_separate_windows():
    guard = bc.BatchCoalescingGuard((READ, DIGEST), bc.BatchCoalescingConfig(2, 2, 1))
    _read(guard, "a.py")
    assert guard.after_dispatch("file_digest", {"path": "b.py"}, ok=True) is None
    assert guard.before_dispatch("file_digest", {"path": "c.py"}) is None
    assert guard.after_dispatch("file_digest", {"path": "c.py"}, ok=True) is not None
    assert guard.snapshot()["families"] == {"file-read": 1, "digest": 2}


def test_inadmissible_batch_tool_is_never_named():
    guard = bc.BatchCoalescingGuard(
        (READ,), bc.BatchCoalescingConfig(2, 2, 1),
        batch_admissible=lambda _cp, _target: False,
    )
    results = [_read(guard, "%d.py" % index) for index in range(6)]
    assert results == [None] * 6
    assert guard.snapshot()["advisories"] == 0


def test_tools_without_counterparts_are_ignored():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 1))
    for index in range(6):
        args = {"path": "%d.py" % index, "content": "x"}
        assert guard.before_dispatch("file_write", args) is None
        assert guard.after_dispatch("file_write", args, ok=True) is None


# -- canaries through the real agent loop -----------------------------------

def _agent_with(monkeypatch, responses, observe=None, real_dispatch=False):
    prompts, dispatches, events = [], [], []
    original_dispatch = server._agent_dispatch_observed

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    def dispatch(tool, args, **kwargs):
        dispatches.append((tool, dict(args)))
        if real_dispatch:
            return original_dispatch(tool, args, **kwargs)
        if observe is not None:
            return observe(tool, args)
        if tool == "context_pack":
            return "context pack: requested=1 selected=1 errors=0\nbody"
        return "contents of %s: value = 1" % args.get("path", "?")

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(server, "_agent_dispatch_observed", dispatch)
    monkeypatch.setattr(
        server.activity_tracker, "record_event",
        lambda kind, **fields: events.append((kind, fields)),
    )
    for key in (bc.ENV_ADVISORY_AFTER, bc.ENV_REFUSE_AFTER, bc.ENV_MAX_REFUSALS):
        monkeypatch.delenv(key, raising=False)
    return prompts, dispatches, events


def _call(tool, **args):
    return json.dumps({"tool": tool, "args": args})


def _reads(*paths):
    return [_call("file_read", path=path) for path in paths]


def _guard_events(events):
    return [fields for kind, fields in events if kind == "agent_guard"]


def test_canary_single_reads_are_steered_then_refused(monkeypatch):
    paths = ["m%d.py" % index for index in range(7)]
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads(*paths) + [
            _call("context_pack", paths_json=["m6.py", "m7.py"]),
            '{"final":"done"}',
        ],
    )

    server._agent_impl("summarize the modules", max_steps=10)

    read_paths = [args["path"] for tool, args in dispatches if tool == "file_read"]
    # The seventh distinct single read was refused, never dispatched.
    assert read_paths == paths[:6]
    assert ("context_pack", {"paths_json": ["m6.py", "m7.py"]}) in dispatches
    # No advisory before the threshold, advisory from the third distinct read.
    assert "HOST BATCH ADVISORY" not in prompts[2]
    assert "HOST BATCH ADVISORY" in prompts[3]
    # The dispatched result itself reaches the model unchanged, ahead of the advisory.
    assert "contents of m2.py: value = 1\nHOST BATCH ADVISORY" in prompts[3]
    assert 'HOST BATCH GUARD (batch_coalescing): file_read was not run' in prompts[7]
    assert 'context_pack {"paths_json": ["m6.py"]}' in prompts[7]
    actions = [event["action"] for event in _guard_events(events)]
    assert actions == ["advisory"] * 4 + ["refusal"]
    assert all(event["batch_tool"] == "context_pack" for event in _guard_events(events))


def test_canary_ignored_refusals_end_the_run(monkeypatch):
    paths = ["n%d.py" % index for index in range(9)]
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads(*paths) + ['{"final":"done"}'],
    )

    result = server._agent_impl("summarize the modules", max_steps=12)

    assert [args["path"] for _tool, args in dispatches] == paths[:6]
    assert "after 3 batch-guard refusals; use context_pack" in str(result)
    # The run stopped at the third refusal instead of spending the step budget.
    assert len(prompts) == 9
    assert _guard_events(events)[-1]["action"] == "exhausted"


def test_negative_below_threshold_gets_no_steering(monkeypatch):
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads("a.py", "b.py") + ['{"final":"done"}'],
    )

    server._agent_impl("read two files", max_steps=4)

    assert len(dispatches) == 2
    assert "HOST BATCH" not in "\n".join(prompts)
    assert _guard_events(events) == []


def test_negative_mutating_tools_are_never_steered_or_refused(monkeypatch):
    writes = [
        _call("file_write", path="w%d.txt" % index, content="x", mode="create")
        for index in range(8)
    ]
    prompts, dispatches, events = _agent_with(
        monkeypatch, writes + ['{"final":"done"}'],
        observe=lambda tool, args: "wrote %s" % args.get("path"),
    )

    server._agent_impl("write the files", max_steps=10)

    assert [tool for tool, _args in dispatches].count("file_write") == 8
    assert "HOST BATCH" not in "\n".join(prompts)
    assert _guard_events(events) == []


def test_negative_other_families_do_not_share_the_window(monkeypatch):
    calls = []
    for index in range(4):
        calls.append(_call("file_read", path="r%d.py" % index))
        calls.append(_call("file_read_range", path="q%d.py" % index, start_line=1, end_line=5))
    prompts, dispatches, events = _agent_with(monkeypatch, calls + ['{"final":"done"}'])

    server._agent_impl("inspect", max_steps=10)

    # Eight distinct single calls, but only four in the file_read family:
    # advisories, never a refusal, and file_read_range is never steered.
    assert len(dispatches) == 8
    assert "HOST BATCH GUARD" not in "\n".join(prompts)
    assert [event["tool"] for event in _guard_events(events)] == ["file_read", "file_read"]


def test_negative_new_turn_starts_a_new_window(monkeypatch):
    paths = ["t%d.py" % index for index in range(6)]
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads(*paths) + ['{"final":"first"}'] + _reads("t6.py") + ['{"final":"second"}'],
    )

    server._agent_impl("first turn", max_steps=8)
    server._agent_impl("second turn", max_steps=3)

    assert [args["path"] for _tool, args in dispatches] == paths + ["t6.py"]
    assert "HOST BATCH GUARD" not in "\n".join(prompts)
    assert "HOST BATCH ADVISORY" not in prompts[-1]


def test_negative_state_change_starts_a_new_window(monkeypatch):
    paths = ["s%d.py" % index for index in range(6)]
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads(*paths)
        + [_call("file_write", path="out.txt", content="x", mode="create")]
        + _reads("s6.py")
        + ['{"final":"done"}'],
    )

    server._agent_impl("read, write, re-read", max_steps=10)

    assert ("file_read", {"path": "s6.py"}) in dispatches
    assert "HOST BATCH GUARD" not in "\n".join(prompts)


def test_negative_batch_tool_outside_allowlist_is_never_named(monkeypatch):
    paths = ["u%d.py" % index for index in range(8)]
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads(*paths) + ['{"final":"done"}'],
    )

    server._agent_impl("inspect", max_steps=10, tool_allowlist=["file_read"])

    assert [args["path"] for _tool, args in dispatches] == paths
    assert "HOST BATCH" not in "\n".join(prompts)
    assert _guard_events(events) == []


def test_negative_required_single_tool_keeps_its_single_form(monkeypatch):
    paths = ["v%d.py" % index for index in range(8)]
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads(*paths) + ['{"final":"done"}'],
    )

    server._agent_impl("inspect", max_steps=10, required_tool_names=["file_read"])

    assert len(dispatches) == 8
    assert _guard_events(events) == []


def test_configured_thresholds_apply(monkeypatch):
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads("c0.py", "c1.py", "c2.py") + ['{"final":"done"}'],
    )
    monkeypatch.setenv(bc.ENV_ADVISORY_AFTER, "2")
    monkeypatch.setenv(bc.ENV_REFUSE_AFTER, "2")

    server._agent_impl("inspect", max_steps=5)

    assert [args["path"] for _tool, args in dispatches] == ["c0.py", "c1.py"]
    assert [event["action"] for event in _guard_events(events)] == ["advisory", "refusal"]


# -- review findings: window-scoped refusals, retries, views, ceilings ------

def test_threshold_ceiling_matches_the_agent_step_clamp():
    assert server._AGENT_MAX_STEPS_CEILING == bc.AGENT_STEP_CEILING
    assert server._safe_limit_policy(999, 6, server._AGENT_MAX_STEPS_CEILING) == bc.AGENT_STEP_CEILING
    assert bc.MAX_THRESHOLD == bc.AGENT_STEP_CEILING - 1
    # The largest accepted thresholds still fire inside one clamped turn.
    config = bc.BatchCoalescingConfig(bc.MAX_THRESHOLD, bc.MAX_THRESHOLD, 1)
    guard = bc.BatchCoalescingGuard((READ,), config)
    outcomes = [_read(guard, "%d.py" % index) for index in range(bc.MAX_THRESHOLD + 1)]
    assert isinstance(outcomes[bc.MAX_THRESHOLD - 1], bc.BatchAdvisory)
    assert isinstance(outcomes[bc.MAX_THRESHOLD], bc.BatchRefusal)
    # Advisory on step 19, refusal on step 20: both within the ceiling.
    assert bc.MAX_THRESHOLD + 1 <= bc.AGENT_STEP_CEILING


def test_refusal_count_restarts_with_the_window():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 2))
    _read(guard, "a.py")
    _read(guard, "b.py")
    first = _read(guard, "c.py")
    assert isinstance(first, bc.BatchRefusal) and first.refusals == 1
    # The model complied: a successful batch call starts a new window.
    assert guard.after_dispatch("context_pack", {"paths_json": ["c.py"]}, ok=True) is None
    _read(guard, "d.py")
    _read(guard, "e.py")
    again = _read(guard, "f.py")
    assert isinstance(again, bc.BatchRefusal)
    assert again.refusals == 1 and not again.exhausted
    assert "Refusal 1 of 2 in this window" in again.render()
    assert "run ends here" not in again.render()

    # A state-change reset also restarts the count.
    guard.reset()
    _read(guard, "g.py")
    _read(guard, "h.py")
    assert _read(guard, "i.py").refusals == 1
    assert guard.snapshot()["window_refusals"] == {"file-read": 1}


def test_retry_of_a_failed_target_is_never_refused():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 1))
    assert _read(guard, "x.py", ok=False) is None
    _read(guard, "a.py")
    _read(guard, "b.py")
    assert guard.before_dispatch("file_read", {"path": "x.py"}) is None
    # The successful retry is not counted as a new target either.
    assert guard.after_dispatch("file_read", {"path": "x.py"}, ok=True) is None
    assert guard.snapshot()["families"] == {"file-read": 2}
    assert isinstance(guard.before_dispatch("file_read", {"path": "y.py"}), bc.BatchRefusal)


def test_targets_returned_by_a_batch_call_can_be_read_singly():
    guard = bc.BatchCoalescingGuard((READ,), bc.BatchCoalescingConfig(2, 2, 1))
    guard.after_dispatch(
        "context_pack", {"paths_json": json.dumps(["p.py", "q.py", "r.py"])}, ok=True,
    )
    _read(guard, "a.py")
    _read(guard, "b.py")
    # p/q/r were returned by the batch: reading them singly (for example to
    # see what the batch view clipped) is neither counted nor refused.
    for path in ("p.py", "q.py", "r.py"):
        assert _read(guard, path) is None
    assert guard.snapshot()["families"] == {"file-read": 2}
    assert isinstance(_read(guard, "s.py"), bc.BatchRefusal)
    # A failed batch call covers nothing.
    guard.after_dispatch("context_pack", {"paths_json": ["t.py"]}, ok=False)
    assert isinstance(guard.before_dispatch("file_read", {"path": "t.py"}), bc.BatchRefusal)


def test_resolver_merges_relative_absolute_and_symlinked_spellings(tmp_path, monkeypatch):
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "src" / "a.py")
    identity = server._agent_batch_target_identity
    assert identity("src/a.py") == identity(str(tmp_path / "src" / "a.py"))
    assert identity("link.py") == identity("src/a.py")
    # Not-yet-existing and unresolvable spellings never raise.
    assert identity("missing/../b.py") == str(tmp_path / "b.py")
    assert isinstance(identity("bad\x00name"), str)

    guard = bc.BatchCoalescingGuard(
        (READ,), bc.BatchCoalescingConfig(2, 2, 1), resolve_target=identity,
    )
    _read(guard, "src/a.py")
    _read(guard, "src/b.py")
    for spelling in (str(tmp_path / "src" / "a.py"), "link.py", "./src/../src/a.py"):
        assert guard.before_dispatch("file_read", {"path": spelling}) is None
    assert isinstance(guard.before_dispatch("file_read", {"path": "src/c.py"}), bc.BatchRefusal)


def test_steering_texts_state_the_view_budget_and_batch_size():
    guard = bc.BatchCoalescingGuard(
        (READ,), bc.BatchCoalescingConfig(2, 3, 1), view_chars=6000,
    )
    _read(guard, "a.py")
    advisory = _read(guard, "b.py")
    _read(guard, "c.py")
    refusal = _read(guard, "d.py")
    assert bc.recommended_batch_size(6000) == 4
    assert bc.recommended_batch_size(100) == 1
    for text in (advisory.render(), refusal.render()):
        assert "within 6000 characters" in text
        assert "at most 4 targets per call" in text
        assert "in this turn" not in text
    with pytest.raises(ValueError):
        bc.BatchCoalescingGuard((READ,), view_chars=-1)


def test_sectioned_view_keeps_every_pack_file_visible():
    from sonder_runtime.domain.agents.observation_prompt import fit_sectioned_text

    prefix = server._CONTEXT_PACK_SECTION_PREFIX
    sections = [
        "%s%d/3: f%d.py =====\nstatus: ok\n\n%s" % (prefix, n, n, ("%d" % n) * 3000)
        for n in (1, 2, 3)
    ]
    text = "context pack: requested=3\n\n" + "\n\n".join(sections)
    view = fit_sectioned_text(text, 6000, prefix, clip_hint="read the rest singly")
    assert len(view) <= 6000
    assert view.startswith("[HOST VIEW:")
    for n in (1, 2, 3):
        assert "%s%d/3: f%d.py =====" % (prefix, n, n) in view
        assert ("%d" % n) * 200 in view
    assert view.count("host clipped this section") == 3
    # Under budget: unchanged.  No sections: plain two-ended clip.
    assert fit_sectioned_text("short", 6000, prefix) == "short"
    plain = fit_sectioned_text("z" * 9000, 6000, prefix)
    assert len(plain) <= 6000 and "observation compacted by host" in plain
    # Small sections keep their full text; the long one absorbs the clip.
    mixed = "\n".join([
        "%s1/2: s.py =====\nsmall body" % prefix,
        "%s2/2: l.py =====\n%s" % (prefix, "L" * 9000),
    ])
    fitted = fit_sectioned_text(mixed, 6000, prefix)
    assert "small body" in fitted and fitted.count("host clipped this section") == 1


def test_canary_refusal_count_restarts_after_compliant_batch(monkeypatch):
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads("a.py", "b.py", "c.py")
        + [_call("context_pack", paths_json=["c.py"])]
        + _reads("d.py", "e.py", "f.py")
        + ['{"final":"done"}'],
    )
    monkeypatch.setenv(bc.ENV_ADVISORY_AFTER, "2")
    monkeypatch.setenv(bc.ENV_REFUSE_AFTER, "2")
    monkeypatch.setenv(bc.ENV_MAX_REFUSALS, "2")

    result = server._agent_impl("inspect", max_steps=10)

    assert "batch-guard refusals" not in str(result)
    read_paths = [args["path"] for tool, args in dispatches if tool == "file_read"]
    assert read_paths == ["a.py", "b.py", "d.py", "e.py"]
    refusals = [event for event in _guard_events(events) if event["action"] != "advisory"]
    assert [(event["action"], event["refusals"]) for event in refusals] == [
        ("refusal", 1), ("refusal", 1),
    ]
    assert "Refusal 1 of 2 in this window" in prompts[-1]


def test_canary_host_required_retry_is_dispatched(monkeypatch):
    attempts = {"x.py": 0}

    def observe(tool, args):
        path = args.get("path", "")
        if tool == "file_read" and path == "x.py":
            attempts["x.py"] += 1
            if attempts["x.py"] == 1:
                return "ERROR: transient read failure"
        return "contents of %s: value = 1" % path

    paths = ["m%d.py" % index for index in range(6)]
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads("x.py", *paths) + ['{"final":"done"}']
        + _reads("x.py") + ['{"final":"done"}'],
        observe=observe,
    )
    monkeypatch.setattr(server, "_repository_read_only_error", lambda *a, **k: "")

    result = server._agent_impl(
        "review the modules", max_steps=13,
        read_only=True, require_file_evidence=True,
    )

    assert attempts["x.py"] == 2
    assert "EVIDENCE_REQUIRED" not in str(result)
    assert "done" in str(result)
    assert all(event["action"] == "advisory" for event in _guard_events(events))


def test_canary_pack_view_shows_every_file_and_marks_clips(monkeypatch, tmp_path):
    for name in ("p1.py", "p2.py", "p3.py"):
        (tmp_path / name).write_text(name[1] * 3000 + "\n", encoding="utf-8")
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        [_call("context_pack", paths_json=["p1.py", "p2.py", "p3.py"]), '{"final":"done"}'],
        real_dispatch=True,
    )

    server._agent_impl(
        "inspect", max_steps=4, read_only=True, project=str(tmp_path),
    )

    view = prompts[1]
    assert "[HOST VIEW:" in view
    for index, name in enumerate(("p1.py", "p2.py", "p3.py"), 1):
        assert "CONTEXT FILE %d/3: %s" % (index, str(tmp_path / name)) in view
        assert name[1] * 200 in view
    assert view.count("host clipped this section") == 3


def test_negative_argument_aware_tool_policy_is_never_steered(monkeypatch):
    paths = ["w%d.py" % index for index in range(8)]
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads(*paths) + ['{"final":"done"}'],
    )

    server._agent_impl("inspect", max_steps=10, tool_policy=lambda *_a: "")

    assert [args["path"] for _tool, args in dispatches] == paths
    assert "HOST BATCH" not in "\n".join(prompts)
    assert _guard_events(events) == []


def test_negative_abort_on_failure_tool_keeps_its_single_form(monkeypatch):
    paths = ["z%d.py" % index for index in range(8)]
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads(*paths) + ['{"final":"done"}'],
    )

    server._agent_impl(
        "inspect", max_steps=10, abort_on_tool_failure_names=["file_read"],
    )

    assert [args["path"] for _tool, args in dispatches] == paths
    assert _guard_events(events) == []


def test_canary_read_only_project_run_names_the_rebased_path(monkeypatch, tmp_path):
    names = ["k%d.py" % index for index in range(7)]
    for name in names:
        (tmp_path / name).write_text("value = 1\n", encoding="utf-8")
    prompts, dispatches, events = _agent_with(
        monkeypatch,
        _reads(*names)
        + [_call("context_pack", paths_json=["k6.py"]), '{"final":"done"}'],
        real_dispatch=True,
    )

    result = server._agent_impl(
        "inspect", max_steps=10, read_only=True, project=str(tmp_path),
    )

    read_paths = [args["path"] for tool, args in dispatches if tool == "file_read"]
    # Dispatched arguments carry the host's project-rebased paths.
    assert read_paths == [str(tmp_path / name) for name in names[:6]]
    rebased = str(tmp_path / "k6.py")
    assert 'context_pack {"paths_json": [%s]}' % json.dumps(rebased) in prompts[7]
    # The recommended batch tool is admitted for the project-bound run.
    assert [tool for tool, _args in dispatches].count("context_pack") == 1
    assert "status: ok" in prompts[8]
    assert "done" in str(result)


def test_canary_absolute_spelling_of_a_read_target_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: tmp_path)
    paths = ["y%d.py" % index for index in range(6)]
    absolute = str(tmp_path / "y0.py")
    prompts, dispatches, events = _agent_with(
        monkeypatch, _reads(*paths, absolute) + ['{"final":"done"}'],
    )

    server._agent_impl("inspect", max_steps=9)

    # A plain (project-less) run: the host resolver maps the absolute
    # spelling to the already-read relative target.
    assert [args["path"] for _tool, args in dispatches] == paths + [absolute]
    assert "HOST BATCH GUARD" not in "\n".join(prompts)
