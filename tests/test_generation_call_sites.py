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
    unseen = [name for name in calls.CHOKE_POINTS + calls.TEXT_ENTRY_POINTS if name not in seen]
    assert not unseen, "no site references %s: the scan has stopped seeing it" % unseen
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
    problems = []
    for key, entry in calls.REGISTRY.items():
        if entry.cls != calls.EXEC or not calls.needs_refusal(key[2]):
            continue
        if scan.sites[key].refused:
            continue
        if not entry.guarded_by:
            problems.append("%s :: %s :: %s has no refusal before the call" % key)
            continue
        for guard in entry.guarded_by:
            path, qualname = guard.split("::", 1)
            if calls.function_refuses(path, qualname) is not True:
                problems.append("%s guards %s :: %s :: %s but does not refuse" % ((guard,) + key))
    assert not problems, "\n".join(problems) + "\n" + HOW


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
