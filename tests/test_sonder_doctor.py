"""Offline, deterministic tests for the read-only ``sonder doctor`` report.

Every collaborator is injected as a fake check callable -- no DB, no network,
no model, no clock, no subprocess. The default (production) checks are not
exercised here; the module's contract is that ``run_doctor`` faithfully rolls up
whatever registry it is handed and never lets one check abort the report.
"""
import sonder_doctor


def _ok(detail=""):
    return lambda: {"status": "ok", "detail": detail}


def _warn(detail=""):
    return lambda: {"status": "warn", "detail": detail}


def _fail(detail=""):
    return lambda: {"status": "fail", "detail": detail}


def _boom(message="kaboom"):
    def check():
        raise RuntimeError(message)

    return check


def test_all_ok_rolls_up_to_ok():
    report = sonder_doctor.run_doctor(
        {"a": _ok("fine"), "b": _ok(), "c": _ok()}
    )
    assert report["overall"] == "ok"
    assert [c["name"] for c in report["checks"]] == ["a", "b", "c"]
    assert all(c["status"] == "ok" for c in report["checks"])


def test_any_warn_without_fail_rolls_up_to_warn():
    report = sonder_doctor.run_doctor(
        {"a": _ok(), "b": _warn("hygiene"), "c": _ok()}
    )
    assert report["overall"] == "warn"
    warn_entry = next(c for c in report["checks"] if c["name"] == "b")
    assert warn_entry == {"name": "b", "status": "warn", "detail": "hygiene"}


def test_any_fail_dominates_warn_and_ok():
    report = sonder_doctor.run_doctor(
        {"a": _ok(), "b": _warn(), "c": _fail("broken")}
    )
    assert report["overall"] == "fail"


def test_raising_check_becomes_captured_fail_not_exception():
    # The whole point: a check that raises must not propagate. It becomes a
    # ``fail`` entry whose detail names the exception, and later checks still run.
    report = sonder_doctor.run_doctor(
        {"a": _ok(), "boom": _boom("disk gone"), "z": _ok("still ran")}
    )
    assert report["overall"] == "fail"
    boom = next(c for c in report["checks"] if c["name"] == "boom")
    assert boom["status"] == "fail"
    assert "RuntimeError" in boom["detail"]
    assert "disk gone" in boom["detail"]
    # The check after the exploding one still executed.
    tail = next(c for c in report["checks"] if c["name"] == "z")
    assert tail == {"name": "z", "status": "ok", "detail": "still ran"}


def test_skipped_is_neutral_all_skipped_is_ok():
    def skip_missing():
        return {"status": "skipped", "detail": "skipped: collaborator absent"}

    report = sonder_doctor.run_doctor(
        {"a": skip_missing, "b": skip_missing}
    )
    assert report["overall"] == "ok"
    assert all(c["status"] == "skipped" for c in report["checks"])


def test_skipped_does_not_mask_a_warn():
    report = sonder_doctor.run_doctor(
        {"a": lambda: "skipped", "b": _warn("something")}
    )
    assert report["overall"] == "warn"


def test_ordering_is_preserved_for_pairs_and_bare_callables():
    def first():
        return "ok"

    def second():
        return "ok"

    report = sonder_doctor.run_doctor([("later", first), second])
    # Pair name is honoured; bare callable falls back to __name__.
    assert [c["name"] for c in report["checks"]] == ["later", "second"]


def test_result_shapes_are_normalized():
    # Bare string, (status, detail) tuple, bool True, and a name-overriding
    # mapping all normalize to {name, status, detail}.
    checks = [
        ("s", lambda: "warn"),
        ("t", lambda: ("fail", "detail here")),
        ("b", lambda: True),
        ("m", lambda: {"name": "renamed", "status": "ok", "detail": "d"}),
    ]
    report = sonder_doctor.run_doctor(checks)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["s"]["status"] == "warn"
    assert by_name["t"] == {"name": "t", "status": "fail", "detail": "detail here"}
    assert by_name["b"]["status"] == "ok"
    # Mapping overrode the registry name.
    assert "renamed" in by_name
    assert by_name["renamed"]["status"] == "ok"


def test_render_report_summarizes_overall_and_each_check():
    report = sonder_doctor.run_doctor(
        {"config": _ok("loaded"), "ollama": _fail("127.0.0.1: refused")}
    )
    text = sonder_doctor.render_report(report)
    lines = text.splitlines()
    assert lines[0] == "sonder doctor: FAIL"
    body = "\n".join(lines[1:])
    assert "config" in body and "loaded" in body
    assert "ollama" in body and "refused" in body
    # Rendering is pure text: no ANSI escape sequences.
    assert "\x1b[" not in text


def test_render_report_handles_empty_registry():
    report = sonder_doctor.run_doctor({})
    assert report == {"overall": "ok", "checks": []}
    text = sonder_doctor.render_report(report)
    assert "sonder doctor: OK" in text
    assert "no checks run" in text


def test_run_doctor_is_read_only_does_not_mutate_registry_or_call_extra():
    calls = []

    def spy_ok():
        calls.append("ok")
        return "ok"

    registry = {"only": spy_ok}
    snapshot = dict(registry)
    report = sonder_doctor.run_doctor(registry)
    # The registry passed in is untouched (no injected keys, same identity map).
    assert registry == snapshot
    # Each check is invoked exactly once, nothing extra.
    assert calls == ["ok"]
    assert report["overall"] == "ok"


def test_default_checks_registry_is_read_only_pairs_and_stable():
    # We do not run the defaults (they touch env/db/network); we only assert the
    # registry is well-formed, ordered, and returns fresh pairs each call so a
    # caller mutating the list cannot corrupt the module.
    first = sonder_doctor.default_checks()
    second = sonder_doctor.default_checks()
    names = [name for name, _ in first]
    assert names == [
        "config",
        "storage_state",
        "storage_models",
        "self_heal",
        "memory_quality",
        "runtime_policy",
        "ollama",
    ]
    assert all(callable(fn) for _, fn in first)
    first.append(("extra", lambda: "ok"))
    assert len(sonder_doctor.default_checks()) == len(second)
