from sonder_runtime.adapters.playbook_search import duplicate_candidates


def test_existing_fts_sanitizes_query_and_returns_only_matching_candidates():
    notes = [{"id": "build", "title": "Compiler setup", "body": "Pin the build compiler."},
             {"id": "shell", "title": "Quoting", "body": "Quote PowerShell literal paths."}]
    assert duplicate_candidates('compiler" OR ***', notes)[0]["id"] == "build"
    assert duplicate_candidates("quux-no-matching", notes) == []
