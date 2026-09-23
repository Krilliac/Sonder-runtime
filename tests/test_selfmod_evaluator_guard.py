from __future__ import annotations

import selfmod
import scripts.nightly_selfmod as nightly_selfmod


def test_evaluator_and_verifier_truth_sources_are_protected():
    protected = (
        "verifiers.py",
        "promotion_eval.py",
        "eval_retrieval.py",
        "ruff_verifier.py",
        "node_verifier.py",
        "json_schema_verifier.py",
        "sql_verifier.py",
        "scripts/nightly_selfmod.py",
        "scripts/nightly_self_improve.py",
        "scripts/check_architecture.py",
        "scripts/generate_documentation_catalogs.py",
        "tests/test_verifiers.py",
    )
    assert all(selfmod.is_protected_path(path) for path in protected)
    assert not set(protected) & set(nightly_selfmod._eligible_candidate_files())
