from sonder_runtime.domain.memory.playbooks import (
    PlaybookPolicy, approval_status, duplicate_match, make_entry, quality_findings,
    similarity, slugify,
)


def test_policy_defaults_and_slugging():
    assert PlaybookPolicy().approval == "required"
    assert slugify("Build Procedures") == "build-procedures"
    assert similarity("run tests now", "run tests now") == 1.0


def test_categories_and_approval_modes():
    policy = PlaybookPolicy(approval="owner_corrections_auto")
    assert approval_status(policy, tainted=False, owner_correction=True) == "approved"
    assert approval_status(policy, tainted=True, owner_correction=True) == "proposed"
    entry = make_entry("builds", "procedure", "Build", "Run tests in order.", tainted=True)
    assert entry["status"] == "proposed"


def test_procedures_allow_multiple_steps():
    entry = make_entry(
        "builds", "procedure", "Build procedure",
        "1. Configure the tree.\n2. Build the target.\n3. Run the focused tests.",
    )
    assert entry["category"] == "procedure"
    assert not any("multi_claim" in item for item in quality_findings(entry["title"], entry["body"]))


def test_duplicate_and_near_duplicate_detection():
    existing = [{"id": "one", "search_text": "Use Ninja for incremental builds"}]
    assert duplicate_match("Use Ninja for incremental builds", existing)["id"] == "one"
    assert duplicate_match("Use Ninja for incremental builds", existing)["similarity"] == 1.0


def test_secret_redaction_and_metadata_validation():
    entry = make_entry("security", "pitfall", "Credential handling", "Use api_key=supersecretvalue in local config.")
    assert "supersecretvalue" not in entry["body"]
    try:
        make_entry("security", "unknown", "Bad", "Enough words here")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown categories must fail")


def test_invalid_policy_is_rejected():
    for kwargs in ({"approval": "never"}, {"max_entry_bytes": 0}, {"max_topics": 0}):
        try:
            PlaybookPolicy(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"invalid policy accepted: {kwargs}")
