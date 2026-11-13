"""Ratchet: every generation call site is classified, and the classification holds.

The tool-lane deny list refuses a denied model at the bottom of the generation
stack unless the call site declares itself text-only.  This test keeps that
default honest: a new place that generates with a model must be classified in
``tests/support/generation_call_sites.py`` before it can merge, a TEXT site
must say ``text_only=True`` literally, an EXEC site must not, and an EXEC
consumer of a text entry point (which the bottom check cannot see) must refuse
first.  It also fails when a choke point is renamed or a scanned root goes
missing, so it cannot pass by finding nothing.
"""
from __future__ import annotations

import ast
import textwrap

import pytest

from tests.support import generation_call_sites as calls

HOW = (
    "classify each site in tests/support/generation_call_sites.py: TEXT if the "
    "reply is only returned or shown (pass text_only=True to a choke point), "
    "EXEC if it may be run, applied, written or turned into a tool call (leave "
    "the default, and refuse first when calling a text entry point); see "
    + calls.DOC_POINTER
)


@pytest.fixture(scope="module")
def scan():
    return calls.collect()


def _fmt(keys):
    return "\n".join("  %s :: %s :: %s" % key for key in sorted(keys))


# ---- the scan itself ---------------------------------------------------------

def test_every_root_is_scanned_and_parses(scan):
    for label, _pattern, floor in calls.ROOTS:
        assert len(scan.files[label]) >= floor, (
            "root %r yielded %d files (floor %d): the scan is not seeing the tree"
            % (label, len(scan.files[label]), floor)
        )
    assert scan.unparseable == [], "unparseable source files:\n" + "\n".join(scan.unparseable)


def test_choke_points_and_text_entry_points_exist_and_are_seen(scan):
    defined = scan.definitions["server.py"]
    missing = [name for name in calls.CHOKE_POINTS + calls.TEXT_ENTRY_POINTS if name not in defined]
    assert not missing, "renamed or removed (update the scanner and registry): %s" % missing
    seen = {callee for _path, _qualname, callee in scan.sites}
    unseen = {name for name in calls.CHOKE_POINTS + calls.TEXT_ENTRY_POINTS if name not in seen}
    assert unseen == calls.UNREFERENCED_ENTRY_POINTS, (
        "no site references %s (or UNREFERENCED_ENTRY_POINTS is stale): the scan has stopped seeing it"
        % sorted(unseen ^ calls.UNREFERENCED_ENTRY_POINTS)
    )
    assert calls.GATEWAY_GENERATE in seen
    assert "raw-gateway:" + calls.RAW_GATEWAY_NAME in seen


# ---- the registry --------------------------------------------------------------

def test_every_generation_call_site_is_classified(scan):
    unlisted = set(scan.sites) - set(calls.REGISTRY)
    assert not unlisted, "unclassified generation call sites:\n%s\n%s" % (_fmt(unlisted), HOW)


def test_no_registry_entry_is_stale_and_counts_match(scan):
    stale = set(calls.REGISTRY) - set(scan.sites)
    assert not stale, "registry entries with no site (remove or rename them):\n" + _fmt(stale)
    wrong = {
        key: (entry.count, scan.sites[key].count)
        for key, entry in calls.REGISTRY.items() if scan.sites[key].count != entry.count
    }
    assert not wrong, "site counts changed (registered, found): %s\n%s" % (wrong, HOW)


def test_every_entry_has_a_known_class_and_a_reason():
    for key, entry in calls.REGISTRY.items():
        assert entry.cls in calls.CLASSES, key
        assert entry.reason.strip(), key
        assert entry.count > 0, key
        assert not entry.guarded_by or entry.cls == calls.EXEC, key


def test_gap_entries_are_only_the_allowed_ones():
    gaps = {key for key, entry in calls.REGISTRY.items() if entry.cls == calls.GAP}
    assert gaps <= calls.ALLOWED_GAPS, (
        "new GAP entries (refuse first and classify EXEC):\n" + _fmt(gaps - calls.ALLOWED_GAPS)
    )
    assert calls.ALLOWED_GAPS <= gaps, "stale ALLOWED_GAPS:\n" + _fmt(calls.ALLOWED_GAPS - gaps)


@pytest.mark.skipif(not calls.DECLARATIONS_ENFORCED, reason="inventory mode: text_only not yet threaded")
def test_text_sites_declare_text_only_and_exec_sites_do_not(scan):
    problems = []
    for key, entry in calls.REGISTRY.items():
        callee = key[2]
        if callee not in calls.FLAGGED_CHOKE_POINTS:
            continue
        site = scan.sites[key]
        if entry.cls == calls.TEXT and site.text_only != site.count:
            problems.append("TEXT without a literal text_only=True: %s :: %s :: %s" % key)
        if entry.cls in (calls.EXEC, calls.GAP) and site.text_only:
            problems.append("EXEC passing text_only=True: %s :: %s :: %s" % key)
    assert not problems, "\n".join(problems) + "\n" + HOW


@pytest.mark.skipif(not calls.DECLARATIONS_ENFORCED, reason="inventory mode: text_only not yet threaded")
def test_every_text_only_declaration_is_registered(scan):
    text_scopes = {
        (path, qualname) for (path, qualname, callee), entry in calls.REGISTRY.items()
        if entry.cls == calls.TEXT and callee in calls.FLAGGED_CHOKE_POINTS
        and scan.sites[(path, qualname, callee)].text_only
    }
    unregistered = [
        item for item in scan.text_only_calls
        if item[:2] not in text_scopes and item not in calls.TEXT_ONLY_DECLARATIONS
    ]
    assert not unregistered, "text_only=True outside a registered TEXT site:\n" + _fmt(unregistered)
    unused = set(calls.TEXT_ONLY_DECLARATIONS) - set(scan.text_only_calls)
    assert not unused, "stale TEXT_ONLY_DECLARATIONS:\n" + _fmt(unused)


def test_exec_consumers_the_bottom_check_cannot_see_refuse_first(scan):
    """A refusal counts only when its result stops the function and it judges the call's selector."""
    problems = []
    for key, entry in calls.REGISTRY.items():
        if entry.cls != calls.EXEC or not calls.needs_refusal(key[2]):
            continue
        site = scan.sites[key]
        if entry.guarded_by:
            # Every guard must refuse and judge: one judging guard does not cover another.
            judged, unrefused = calls.guards_judge(entry.guarded_by, site.selectors)
            for guard in unrefused:
                problems.append("%s guards %s :: %s :: %s but no refusal stops it" % ((guard,) + key))
            refused = True
        else:
            refused, judged = site.refused, site.judged
        if not refused:
            problems.append("%s :: %s :: %s has no refusal that stops the function before the call" % key)
        elif not judged and not entry.selector_reason:
            problems.append(
                "%s :: %s :: %s: the refusal does not judge the selector the call passes (%s); "
                "judge that expression or give selector_reason" % (key + (sorted(site.selectors),))
            )
        elif judged and entry.selector_reason:
            problems.append("%s :: %s :: %s: stale selector_reason (the refusal judges the selector)" % key)
    assert not problems, "\n".join(problems) + "\n" + HOW


def test_selector_reasons_only_on_exec_entries():
    for key, entry in calls.REGISTRY.items():
        assert not entry.selector_reason or entry.cls == calls.EXEC, key


# ---- the scanner on synthetic sources -----------------------------------------

def _scan_source(tmp_path, monkeypatch, relpath, source):
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    monkeypatch.setattr(calls, "REPO_ROOT", tmp_path)
    return calls.collect({"scripts": [path]}).sites


def test_scanner_sees_calls_references_lookups_imports_and_paths(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/new_job.py", '''
        """Mentions /api/chat and _make_generate in a docstring only."""
        import functools
        import server
        from server import _generate_text as quiet

        def run(prompt):
            gen = server._make_generate("m", "", 0.2, 64, 0)
            return gen(prompt)

        def lookup():
            return getattr(server, "_make_generate"), getattr(server, "ensemble_answer")

        def partial():
            return functools.partial(server._generate_text, text_only=True)

        URL = "http://127.0.0.1:11434/api/generate"
        MESSAGE = "POST /api/chat failed"
    ''')
    assert sites[("scripts/new_job.py", "run", "_make_generate")].kinds == {"call"}
    assert sites[("scripts/new_job.py", "lookup", "_make_generate")].kinds == {"str"}
    assert sites[("scripts/new_job.py", "lookup", "ensemble_answer")].kinds == {"str"}
    assert sites[("scripts/new_job.py", "<module>", "_generate_text")].kinds == {"import"}
    partial = sites[("scripts/new_job.py", "partial", "_generate_text")]
    assert partial.kinds == {"ref"} and partial.text_only == 1
    assert ("scripts/new_job.py", "<module>", "/api/generate") in sites
    # Docstrings and prose messages are not sites.
    assert ("scripts/new_job.py", "<module>", "/api/chat") not in sites
    assert ("scripts/new_job.py", "<module>", "_make_generate") not in sites


def test_scanner_credits_a_refusal_in_the_function_or_a_same_file_helper(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/new_job.py", '''
        from sonder_runtime.adapters import tool_lane_guard

        def _refusal(server):
            return tool_lane_guard.selector_refusal("code", server.TIERS)

        def checked(server, prompt):
            if _refusal(server):
                return ""
            return server.ensemble_answer(prompt)

        def outer(server):
            if tool_lane_guard.selector_refusal("", server.TIERS):
                return None

            def inner(prompt):
                return server.sonder(prompt)
            return inner

        def unchecked(server, prompt):
            return server.ensemble_answer(prompt)
    ''')
    assert sites[("scripts/new_job.py", "checked", "ensemble_answer")].refused
    assert sites[("scripts/new_job.py", "outer.inner", "sonder")].refused
    assert not sites[("scripts/new_job.py", "unchecked", "ensemble_answer")].refused
    # The helper judges 'code' but the call takes the ensemble's default tiers.
    assert not sites[("scripts/new_job.py", "checked", "ensemble_answer")].judged
    # An empty selector judges the default route the bare call takes.
    assert sites[("scripts/new_job.py", "outer.inner", "sonder")].judged


TEXT_ONLY_WRAPPERS = '''
    import server

    def offload_exec(p):
        exec(server.offload(p, tier="code"))

    def offload_tier_exec(p):
        exec(server._offload_tier_impl(p, tier="code"))

    def sonder_impl_exec(p):
        exec(server._sonder_impl(p))

    def serialized_exec(p):
        exec(server._sonder_impl_serialized(p))

    def answer_exec(conn, p):
        exec(server._answer(conn, p, "m", "", 0.2, 64, 0, None, "", None)[0])

    def history_exec(p):
        exec(server._answer_with_history_impl(p, []))

    def vision_exec(p):
        exec(server.vision_analyze("x.png", p))

    def vision_impl_exec(p):
        exec(server._vision_analyze_impl("x.png", p))
'''


def test_scanner_sees_every_text_only_wrapper(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", TEXT_ONLY_WRAPPERS)
    expected = {
        "offload_exec": "offload", "offload_tier_exec": "_offload_tier_impl",
        "sonder_impl_exec": "_sonder_impl", "serialized_exec": "_sonder_impl_serialized",
        "answer_exec": "_answer", "history_exec": "_answer_with_history_impl",
        "vision_exec": "vision_analyze", "vision_impl_exec": "_vision_analyze_impl",
    }
    for function, callee in expected.items():
        site = sites[("scripts/evil.py", function, callee)]
        assert site.kinds == {"call"} and not site.refused, function
        assert calls.needs_refusal(callee), callee


def test_a_new_text_only_wrapper_site_fails_the_ratchet(tmp_path, monkeypatch):
    """The registry check itself rejects ``exec(server.offload(...))`` in a new script."""
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", TEXT_ONLY_WRAPPERS)
    unlisted = set(sites) - set(calls.REGISTRY)
    assert ("scripts/evil.py", "offload_exec", "offload") in unlisted


def test_scanner_attributes_aliases_partials_and_gateway_calls(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", '''
        import functools
        import server
        from server import ensemble_answer as ea

        ask = server.offload
        later = functools.partial(server.ensemble_answer, tiers="code")

        def aliased(p):
            exec(ea(p, tiers="code"))

        def assigned(p):
            exec(ask(p))

        def via_partial(p):
            exec(later(p))

        def keywords(gateway, req, ctx):
            return gateway.generate(request=req, context=ctx)

        def three(gateway, req, ctx):
            return gateway.generate(req, ctx, None)

        def raw(application, req, ctx):
            return application.model_gateway

        def hardware(cuda):
            offload = bool(cuda)
            return {"cpu_offload_supported": offload}
    ''')
    aliased = sites[("scripts/evil.py", "aliased", "ensemble_answer")]
    assert aliased.kinds == {"call"} and aliased.selectors == {"'code'"}
    assert sites[("scripts/evil.py", "<module>", "ensemble_answer")].kinds >= {"import"}
    assert sites[("scripts/evil.py", "assigned", "offload")].kinds == {"call"}
    assert sites[("scripts/evil.py", "via_partial", "ensemble_answer")].kinds == {"call"}
    assert ("scripts/evil.py", "keywords", calls.GATEWAY_GENERATE) in sites
    assert ("scripts/evil.py", "three", calls.GATEWAY_GENERATE) in sites
    assert ("scripts/evil.py", "raw", "raw-gateway:" + calls.RAW_GATEWAY_ATTRIBUTE) in sites
    # A local variable that happens to be called ``offload`` is not the tool.
    assert not any(key[1] == "hardware" for key in sites)


def test_scanner_credits_only_a_refusal_that_stops_and_judges_the_selector(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", '''
        import server
        from sonder_runtime.adapters import tool_lane_guard

        def ghost(p):
            tool_lane_guard.denied("totally-unrelated")
            exec(server.ensemble_answer(p, tiers="astra-cyber-27b"))

        def ignored(p, tiers):
            refusal = tool_lane_guard.selector_refusal(tiers, server.TIERS)
            exec(server.ensemble_answer(p, tiers=tiers))

        def other_selector(p, tiers):
            if tool_lane_guard.selector_refusal("code", server.TIERS):
                return
            exec(server.ensemble_answer(p, tiers=tiers))

        def after(p, tiers):
            exec(server.ensemble_answer(p, tiers=tiers))
            if tool_lane_guard.selector_refusal(tiers, server.TIERS):
                return

        def judged(p, tiers):
            if refusal := tool_lane_guard.selector_refusal(tiers, server.TIERS):
                raise SystemExit(refusal)
            exec(server.ensemble_answer(p, tiers=tiers))

        def judged_through_a_name(p, model):
            refusal = tool_lane_guard.selector_refusal(model or "", server.TIERS)
            if refusal:
                return refusal
            exec(server.sonder(p, model_override=model))

        def _refuse(server, tiers):
            if refusal := tool_lane_guard.selector_refusal(tiers, server.TIERS):
                raise SystemExit(refusal)

        def through_a_raising_helper(p, chosen):
            _refuse(server, chosen)
            exec(server.ensemble_answer(p, tiers=chosen))

        def in_a_loop(tasks):
            for task in tasks:
                if tool_lane_guard.selector_refusal("", server.TIERS):
                    continue
                exec(server.sonder(task))
    ''')

    def site(function, callee="ensemble_answer"):
        return sites[("scripts/evil.py", function, callee)]

    assert not site("ghost").refused
    assert not site("ignored").refused
    assert site("other_selector").refused and not site("other_selector").judged
    assert not site("after").refused
    assert site("judged").refused and site("judged").judged
    assert site("judged_through_a_name", "sonder").judged
    assert site("through_a_raising_helper").judged
    assert site("in_a_loop", "sonder").judged


def test_an_unparseable_file_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "scripts" / "broken.py"
    path.parent.mkdir(parents=True)
    path.write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(calls, "REPO_ROOT", tmp_path)
    assert calls.collect({"scripts": [path]}).unparseable


def test_the_registry_spells_every_key_as_the_scanner_does():
    for path, qualname, callee in calls.REGISTRY:
        assert "\\" not in path and not path.startswith("/"), path
        assert qualname == "<module>" or all(part.isidentifier() for part in qualname.split(".")), qualname
        assert (
            callee in calls.CHOKE_POINTS or callee in calls.TEXT_ENTRY_POINTS
            or callee in calls.ENDPOINT_PATHS or callee == calls.GATEWAY_GENERATE
            or callee.startswith("raw-gateway:")
        ), callee
        ast.parse(callee if callee.isidentifier() else "x")


# ---- the scanner credits only a refusal that really stops the call (review 4) ----

EVASIONS = '''
    import server
    from sonder_runtime.adapters import tool_lane_guard as g

    def dead_branch(p, tier):
        if False:
            if r := g.selector_refusal(tier, server.TIERS):
                return r
        exec(server.sonder(p, tier=tier))

    def swallowed(p, tier):
        try:
            if r := g.selector_refusal(tier, server.TIERS):
                raise ValueError(r)
        except ValueError:
            pass
        exec(server.sonder(p, tier=tier))

    def reassigned(p, tier):
        if r := g.selector_refusal(tier, server.TIERS):
            return r
        tier = "astra-cyber-27b"
        exec(server.sonder(p, tier=tier))

    def _check(tier):
        if g.selector_refusal(tier, server.TIERS):
            raise RuntimeError("no")

    def helper_swallowed(p, tier):
        try:
            _check(tier)
        except RuntimeError:
            pass
        exec(server.ensemble_answer(p, tiers=tier))

    def logs_only(p, tier):
        r = g.selector_refusal(tier, server.TIERS)
        if r:
            print(r)
            if p == "never":
                return r
        exec(server.offload(p, tier=tier))

    def direct(p, tier):
        if r := g.selector_refusal(tier, server.TIERS):
            return r
        with open("log") as handle:
            exec(server.sonder(p, tier=tier))

    def through_the_helper(p, tier):
        _check(tier)
        exec(server.ensemble_answer(p, tiers=tier))

    def _dead_return(tier):
        if False:
            return g.selector_refusal(tier, server.TIERS)
        return ""

    def through_a_dead_return(p, tier):
        if _dead_return(tier):
            return
        exec(server.sonder(p, tier=tier))
'''


@pytest.mark.parametrize("function, callee", [
    ("dead_branch", "sonder"),          # the refusal sits under ``if False:``
    ("swallowed", "sonder"),            # raised inside a ``try`` whose ``except`` swallows it
    ("helper_swallowed", "ensemble_answer"),  # a raising helper wrapped in ``try/except: pass``
    ("logs_only", "offload"),           # the ``return`` sits under an unrelated inner condition
    ("through_a_dead_return", "sonder"),  # a helper whose refusal ``return`` is never reached
])
def test_a_refusal_that_does_not_stop_the_call_is_not_credited(tmp_path, monkeypatch, function, callee):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", EVASIONS)
    site = sites[("scripts/evil.py", function, callee)]
    assert not site.refused and not site.judged, function


def test_a_judged_name_assigned_after_the_refusal_is_not_judged(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", EVASIONS)
    site = sites[("scripts/evil.py", "reassigned", "sonder")]
    assert site.refused and not site.judged


def test_a_direct_refusal_still_covers_a_call_nested_after_it(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/evil.py", EVASIONS)
    assert sites[("scripts/evil.py", "direct", "sonder")].judged
    assert sites[("scripts/evil.py", "through_the_helper", "ensemble_answer")].judged


@pytest.mark.parametrize("check", [
    "if g.selector_refusal(tier, server.TIERS) is None: return",
    "if (r := g.selector_refusal(tier, server.TIERS)) and False: return r",
    "if False or g.selector_refusal(tier, server.TIERS): return",
    "if g.require_generation(tier, text_only=True): return",
    "if g.selector_refusal(tier, {}): return",
    "if g.selector_refusal(tier, other.TIERS): return",
])
def test_scanner_does_not_credit_non_stopping_or_wrongly_bound_checks(tmp_path, monkeypatch, check):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/shapes.py", f'''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, tier):
            {check}
            exec(server.sonder(p, tier=tier))
    ''')
    assert not sites[("scripts/shapes.py", "run", "sonder")].refused


def test_scanner_does_not_credit_a_shadowed_refusal(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/shadow.py", '''
        import server
        def selector_refusal(tier, bindings):
            return ""
        def run(p, tier):
            if selector_refusal(tier, server.TIERS): return
            exec(server.sonder(p, tier=tier))
    ''')
    assert not sites[("scripts/shadow.py", "run", "sonder")].refused


@pytest.mark.parametrize("mutation, selector", [
    ("args.tiers = 'astra-cyber-27b'", "args.tiers"),
    ("args['tiers'] = 'astra-cyber-27b'", "args['tiers']"),
    ("args.tiers[0] = 'astra-cyber-27b'", "args.tiers"),
])
def test_scanner_loses_judgement_after_attribute_or_subscript_store(
    tmp_path, monkeypatch, mutation, selector,
):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/mutate.py", f'''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, args):
            if g.selector_refusal({selector}, server.TIERS): return
            {mutation}
            exec(server.ensemble_answer(p, tiers={selector}))
    ''')
    site = sites[("scripts/mutate.py", "run", "ensemble_answer")]
    assert site.refused and not site.judged


@pytest.mark.parametrize("check", [
    "if g.selector_refusal(tier, server.TIERS): return",
    "if r := g.selector_refusal(tier, server.TIERS): return r",
    "r = g.selector_refusal(tier, server.TIERS)\n            if r: return r",
    "if (r := g.selector_refusal(tier, server.TIERS)) or False: return r",
])
def test_scanner_credits_only_supported_truthy_refusal_shapes(tmp_path, monkeypatch, check):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/valid.py", f'''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, tier):
            {check}
            exec(server.sonder(p, tier=tier))
    ''')
    site = sites[("scripts/valid.py", "run", "sonder")]
    assert site.refused and site.judged


def test_scanner_accepts_a_runtime_tier_map_with_an_empty_fallback(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/runtime.py", '''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(rt, p, tier):
            known = getattr(rt, "TIERS", None)
            if g.selector_refusal(tier, known or {}): return
            exec(server.sonder(p, tier=tier))
    ''')
    assert sites[("scripts/runtime.py", "run", "sonder")].judged


def test_scanner_accepts_an_imported_real_tier_map_but_not_a_local_replacement(tmp_path, monkeypatch):
    accepted = _scan_source(tmp_path, monkeypatch, "scripts/imported.py", '''
        import server
        from server import TIERS
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, tier):
            if g.selector_refusal(tier, TIERS): return
            exec(server.sonder(p, tier=tier))
    ''')
    rejected = _scan_source(tmp_path, monkeypatch, "scripts/local.py", '''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        TIERS = {}
        def run(p, tier):
            if g.selector_refusal(tier, TIERS): return
            exec(server.sonder(p, tier=tier))
    ''')
    assert accepted[("scripts/imported.py", "run", "sonder")].judged
    assert not rejected[("scripts/local.py", "run", "sonder")].refused


def test_scanner_does_not_borrow_a_tier_map_from_a_sibling_function(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/sibling.py", '''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def elsewhere(rt):
            known = getattr(rt, "TIERS", None)
        def run(p, tier):
            if g.selector_refusal(tier, known or {}): return
            exec(server.sonder(p, tier=tier))
    ''')
    assert not sites[("scripts/sibling.py", "run", "sonder")].refused


def test_scanner_accepts_a_tier_map_from_an_enclosing_function(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/enclosing.py", '''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def outer(rt, p, tier):
            known = getattr(rt, "TIERS", None)
            def run():
                if g.selector_refusal(tier, known or {}): return
                exec(server.sonder(p, tier=tier))
            return run()
    ''')
    assert sites[("scripts/enclosing.py", "outer.run", "sonder")].judged


def test_scanner_does_not_credit_a_reassigned_tier_map(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/reassigned_map.py", '''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(rt, p, tier):
            known = getattr(rt, "TIERS", None)
            known = {}
            if g.selector_refusal(tier, known or {}): return
            exec(server.sonder(p, tier=tier))
    ''')
    assert not sites[("scripts/reassigned_map.py", "run", "sonder")].refused


def test_scanner_does_not_credit_a_locally_rebound_imported_tiers(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/rebound_tiers.py", '''
        import server
        from server import TIERS
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, tier):
            TIERS = {}
            if g.selector_refusal(tier, TIERS): return
            exec(server.sonder(p, tier=tier))
    ''')
    assert not sites[("scripts/rebound_tiers.py", "run", "sonder")].refused


@pytest.mark.parametrize("shadow, check", [
    ("from sonder_runtime.adapters.tool_lane_guard import prompt_refusal as selector_refusal",
     "selector_refusal(tier, server.TIERS)"),
    ("try: raise ValueError()\n            except ValueError as selector_refusal: pass",
     "selector_refusal(tier, server.TIERS)"),
    ("g.selector_refusal = lambda *args: ''", "g.selector_refusal(tier, server.TIERS)"),
])
def test_scanner_does_not_credit_other_refusal_shadowing(tmp_path, monkeypatch, shadow, check):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/shadowed.py", f'''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, tier):
            {shadow}
            if {check}: return
            exec(server.sonder(p, tier=tier))
    ''')
    assert not sites[("scripts/shadowed.py", "run", "sonder")].refused


@pytest.mark.parametrize("mutation", [
    "setattr(args, 'tiers', 'astra-cyber-27b')",
    "args.update(tiers='astra-cyber-27b')",
])
def test_scanner_loses_judgement_after_a_call_that_can_mutate_the_selector(
    tmp_path, monkeypatch, mutation,
):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/mutating_call.py", f'''
        import server
        from sonder_runtime.adapters import tool_lane_guard as g
        def run(p, args):
            if g.selector_refusal(args.tiers, server.TIERS): return
            {mutation}
            exec(server.ensemble_answer(p, tiers=args.tiers))
    ''')
    site = sites[("scripts/mutating_call.py", "run", "ensemble_answer")]
    assert site.refused and not site.judged


GUARDS = '''
    import server
    from sonder_runtime.adapters import tool_lane_guard as g

    def judging(p, tier):
        if r := g.selector_refusal(tier, server.TIERS):
            return r
        return site(p, tier)

    def judging_another(p, tier):
        if r := g.selector_refusal("code", server.TIERS):
            return r
        return site(p, tier)

    def swallowing(p, tier):
        try:
            if r := g.selector_refusal(tier, server.TIERS):
                return r
        except ValueError:
            pass
        return site(p, tier)

    def site(p, tier):
        exec(server.sonder(p, tier=tier))
'''


def test_every_guard_must_refuse_and_judge(tmp_path, monkeypatch):
    sites = _scan_source(tmp_path, monkeypatch, "scripts/guards.py", GUARDS)
    selectors = sites[("scripts/guards.py", "site", "sonder")].selectors
    assert selectors == {"tier"}
    judging, other, swallowing = ("scripts/guards.py::" + name for name in ("judging", "judging_another", "swallowing"))
    assert calls.guards_judge([judging], selectors) == (True, [])
    # One judging guard does not cover a guard that judges another selector (review 4, M2) ...
    assert calls.guards_judge([judging, other], selectors) == (False, [])
    # ... or one whose refusal a ``try`` swallows (review 4, M7).
    assert calls.guards_judge([judging, swallowing], selectors) == (False, [swallowing])
