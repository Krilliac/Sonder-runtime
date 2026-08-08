from pathlib import Path


def test_bare_pytest_collects_shipped_proposal_tests():
    # If proposals disappears from testpaths, shipped compatibility tests can
    # break while the repository's only CI invocation remains green.
    config = (Path(__file__).resolve().parents[1] / "pytest.ini").read_text(
        encoding="utf-8"
    )
    testpaths = next(
        line.split("=", 1)[1].split()
        for line in config.splitlines()
        if line.strip().startswith("testpaths")
    )
    assert {"tests", "proposals"} <= set(testpaths)
