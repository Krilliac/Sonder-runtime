"""The batch and measurement scripts, on a synthetic repository and synthetic labels."""
import json

import pytest

from scripts import claim_check_batch, claim_check_eval

SOURCE = "class P {\n  void Fill(int n) {\n    if (n > MaxItems) return;\n    var a = new byte[n];\n  }\n}\n"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "P.cs").write_text(SOURCE, newline="\n")
    return root


def test_batch_cli_writes_one_line_per_row_and_prints_counts(repo, tmp_path, capsys):
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    rows = [{"id": "a", "file": "P.cs", "line": 4}, {"id": "b", "file": "P.cs", "line": 90}]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    code = claim_check_batch.main([str(src), str(out), "--root", str(repo)])
    assert code == 0
    written = [json.loads(x) for x in out.read_text().splitlines()]
    assert [w["verdict"] for w in written] == ["flag", "reject"]
    assert "total=2" in capsys.readouterr().out


def test_batch_cli_refuses_a_missing_root(tmp_path, capsys):
    src = tmp_path / "in.jsonl"
    src.write_text("{}\n")
    code = claim_check_batch.main([str(src), str(tmp_path / "o.jsonl"), "--root", str(tmp_path / "x")])
    assert code == 2
    assert "not a directory" in capsys.readouterr().err


def test_eval_counts_labels_by_verdict_without_printing_rows(repo, tmp_path, capsys):
    rows = [
        {"file": "P.cs", "hypothesis": "Line 4 allocates `Fill` unbounded", "label": "CONFIRMED",
         "category": "crash/DoS from client input", "repo_root": str(repo)},
        {"file": "P.cs", "hypothesis": "Line 400 is wrong", "label": "REFUTED",
         "category": "", "repo_root": str(repo)},
        {"file": "P.cs", "hypothesis": "Line 4 uses `Missing`", "label": "REFUTED",
         "category": "", "repo_root": str(repo)},
    ]
    src = tmp_path / "rows.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    result = claim_check_eval.evaluate(src)
    assert result["table"][("CONFIRMED", "flag")] == 1
    assert result["table"][("REFUTED", "reject")] == 2
    assert claim_check_eval.main([str(src)]) == 0
    printed = capsys.readouterr().out
    assert "REFUTED" in printed and "Missing" not in printed and "P.cs" not in printed
    off = claim_check_eval.evaluate(src, symbols="off")
    assert off["table"][("REFUTED", "reject")] == 1
