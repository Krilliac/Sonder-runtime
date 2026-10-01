"""Host evidence, rather than completion prose, drives build candidate ranking."""
from types import SimpleNamespace

from sonder_runtime.adapters import fleet_aggregation as aggregation


def receipt(files=(), run=False, passed=None):
    return SimpleNamespace(produced_files=files, checks_run=run, checks_passed=passed)


def test_build_report_ranks_passed_checks_before_file_count_and_lists_every_worker(monkeypatch):
    monkeypatch.setattr(aggregation.fleet_creations, "inventory_files", lambda folder: ("partial.py",))
    text = aggregation.build_report("/creations/master", {
        "a": "/creations/master/worker-01", "b": "/creations/master/worker-02",
        "c": "/creations/master/worker-03",
    }, [("a", receipt(("one.py", "two.py"))), ("b", receipt(("app.py",), True, True))])
    assert "best_candidate=b in /creations/master/worker-02 (checks passed" in text
    assert "worker=c" in text and "partial.py" in text and "checks=unknown" in text
    assert "checks=not run" in text and "checks=passed" in text


def test_build_report_never_selects_an_empty_candidate():
    text = aggregation.build_report("/creations/master", {"a": "/worker-01"}, [("a", receipt((), True, True))])
    assert "best_candidate=none" in text


def test_build_report_missing_inventory_stays_unknown(monkeypatch):
    def unavailable(folder):
        raise PermissionError("unavailable")
    monkeypatch.setattr(aggregation.fleet_creations, "inventory_files", unavailable)
    text = aggregation.build_report("/creations/master", {"a": "/worker-01"}, [])
    assert "files=unknown" in text and "checks=unknown" in text
    assert "best_candidate=none" in text


def test_build_audit_uses_actual_receipts_and_folders():
    prompt = aggregation.audit_prompt("create a script", [("a", "receipt")],
                                     repository_task=True, project="", creation_root="/creations/master")
    assert "HOST CREATION ROOT: /creations/master" in prompt
    assert "Recommend the best available candidate" in prompt and "receipt" in prompt
    assert "produce a concrete proposal/plan" not in prompt


def test_advice_and_protected_audits_retain_existing_contracts():
    advice = aggregation.audit_prompt("compare ideas", [("a", "proposal")], repository_task=False, project="")
    assert "produce a concrete proposal/plan" in advice and "--- a ---\nproposal" in advice
    protected = aggregation.audit_prompt("inspect", [], repository_task=True, project="/repo", objective_contract="[objective:a]")
    assert "HOST REPOSITORY SCOPE: /repo" in protected and "[objective:a]" in protected
    assert "produce a concrete proposal/plan" not in protected
