from scripts import check_evidence_documents as checker


def test_all_remaining_evidence_documents_have_resolvable_test_links() -> None:
    assert checker.check() == []
