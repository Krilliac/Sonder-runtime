"""Contract tests for the operator/developer doc link checker."""

from scripts import check_doc_links


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_reports_nothing_for_a_resolvable_relative_link(tmp_path, monkeypatch):
    _write(tmp_path / "TARGET.md", "# Target\n")
    _write(tmp_path / "README.md", "[see target](TARGET.md)\n")
    monkeypatch.setattr(check_doc_links, "DOC_ROOTS", (tmp_path,))

    assert check_doc_links.check() == []


def test_flags_a_broken_relative_link(tmp_path, monkeypatch):
    doc = _write(tmp_path / "README.md", "[missing](docs/does-not-exist.md)\n")
    monkeypatch.setattr(check_doc_links, "DOC_ROOTS", (tmp_path,))

    problems = check_doc_links.check()

    assert len(problems) == 1
    assert "does-not-exist.md" in problems[0]
    assert doc.name in problems[0]


def test_ignores_http_anchor_and_repo_absolute_links(tmp_path, monkeypatch):
    _write(
        tmp_path / "README.md",
        "[web](https://example.invalid/x) "
        "[anchor](#section) "
        "[absolute](/etc/passwd) "
        "[mail](mailto:a@example.invalid)\n",
    )
    monkeypatch.setattr(check_doc_links, "DOC_ROOTS", (tmp_path,))

    assert check_doc_links.check() == []


def test_resolves_link_relative_to_its_own_file_not_the_scan_root(tmp_path, monkeypatch):
    _write(tmp_path / "sub" / "TARGET.md", "# Target\n")
    _write(tmp_path / "sub" / "PAGE.md", "[see target](TARGET.md)\n")
    monkeypatch.setattr(check_doc_links, "DOC_ROOTS", (tmp_path,))

    assert check_doc_links.check() == []


def test_main_returns_nonzero_only_when_something_is_broken(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "README.md", "[missing](nope.md)\n")
    monkeypatch.setattr(check_doc_links, "DOC_ROOTS", (tmp_path,))

    assert check_doc_links.main() == 1
    out, err = capsys.readouterr()
    assert "nope.md" in out
    assert "1 broken documentation link" in err
