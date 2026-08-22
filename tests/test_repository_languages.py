from sonder_runtime.domain.repository_languages import RepositoryLanguage, baseline_for, detect_by_extension


def test_repository_language_baseline_covers_required_languages_and_extensions():
    assert baseline_for(RepositoryLanguage.PYTHON).lsp_candidate
    assert detect_by_extension(".cpp").language is RepositoryLanguage.CPP
    assert detect_by_extension("tsx").language is RepositoryLanguage.TYPESCRIPT
