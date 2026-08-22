from sonder_runtime.application.repository_intelligence.navigation import ExpansionRequest, NavigationEvidence, expand


def test_navigation_expansion_is_multi_root_and_bounded():
    evidence = (NavigationEvidence("r1", "a.py", "A", "B"), NavigationEvidence("r1", "b.py", "B", "C"), NavigationEvidence("r2", "c.py", "A", "D"))
    result = expand(evidence, ExpansionRequest(("r1",), ("A",), max_symbols=1, max_hops=2))
    assert len(result) == 1 and result[0].root_id == "r1"
