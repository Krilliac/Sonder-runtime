from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import benchmark_repository_research as research


ROOT = Path(__file__).resolve().parents[1]


def _good_case(fixture: dict) -> dict:
    return {
        "id": fixture["id"],
        "answer": "The requested implementation is present at the cited symbols.",
        "claims": [
            {
                "text": f"{symbol} exists.",
                "status": "exists",
                "citations": [{"path": path, "symbol": symbol}],
            }
            for path, symbol in fixture["required_evidence"]
        ],
        "tool_calls": [
            {"tool": "search", "arguments": {"query": fixture["id"]}},
            {"tool": "open", "arguments": {"case": fixture["id"]}},
        ],
    }


def _good_submission() -> dict:
    return {
        "schema": research.SUBMISSION_SCHEMA,
        "cases": [_good_case(fixture) for fixture in research.DEFAULT_CASES],
    }


class RepositoryResearchBenchmarkTests(unittest.TestCase):
    def test_built_in_fixture_symbols_exist(self) -> None:
        parsed: dict[str, set[str]] = {}
        for fixture in research.DEFAULT_CASES:
            for relative, symbol in fixture["required_evidence"]:
                if relative not in parsed:
                    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
                    parsed[relative] = {
                        node.name
                        for node in tree.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    } | {
                        target.id
                        for node in tree.body
                        if isinstance(node, (ast.Assign, ast.AnnAssign))
                        for target in (
                            node.targets if isinstance(node, ast.Assign) else [node.target]
                        )
                        if isinstance(target, ast.Name)
                    }
                self.assertIn(symbol, parsed[relative], f"missing fixture {relative}:{symbol}")

    def test_clean_exact_evidence_passes_deterministically(self) -> None:
        first = research.evaluate(_good_submission())
        second = research.evaluate(_good_submission())
        self.assertEqual(first, second)
        self.assertEqual(4, first["summary"]["cases_passed"])
        self.assertEqual(0, first["summary"]["false_negative_count"])
        self.assertEqual(0, first["summary"]["unsupported_claim_count"])
        self.assertTrue(first["model_free"])
        self.assertFalse(first["causal_claim"])

    def test_false_no_implementation_claim_is_detected(self) -> None:
        submission = _good_submission()
        target = submission["cases"][0]
        target["answer"] = "I could not find any implementation."
        target["claims"].append(
            {"text": "No implementation exists.", "status": "missing", "citations": []}
        )
        report = research.evaluate(submission)
        case = report["cases"][0]
        self.assertEqual(1, case["false_negative_count"])
        self.assertFalse(case["passed"])

    def test_common_false_negative_wording_is_detected(self) -> None:
        phrases = (
            "This implementation does not exist.",
            "The implementation is absent.",
            "I cannot locate any implementation.",
            "I couldn't find the implementation.",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                submission = _good_submission()
                submission["cases"][0]["answer"] = phrase
                case = research.evaluate(submission)["cases"][0]
                self.assertEqual(1, case["false_negative_count"])
                self.assertFalse(case["passed"])

    def test_invented_path_and_symbol_make_claim_unsupported(self) -> None:
        submission = _good_submission()
        target = submission["cases"][1]
        target["claims"].append(
            {
                "text": "Invented evidence.",
                "status": "exists",
                "citations": [
                    {"path": "scripts/not_real.py", "symbol": "history_status"},
                    {
                        "path": "sonder_runtime/adapters/evaluation_history_store.py",
                        "symbol": "invented_symbol",
                    },
                ],
            }
        )
        case = research.evaluate(submission)["cases"][1]
        self.assertEqual(1, case["unsupported_claim_count"])
        self.assertEqual(
            [research._opaque_evidence_id("scripts/not_real.py")], case["invented_paths"]
        )
        self.assertEqual(
            [research._opaque_evidence_id("invented_symbol")], case["invented_symbols"]
        )
        self.assertNotIn("not_real", json.dumps(case))
        self.assertNotIn('"invented_symbol"', json.dumps(case))
        self.assertFalse(case["passed"])

    def test_unsafe_private_path_is_redacted_from_report(self) -> None:
        submission = _good_submission()
        secret_path = "X:\\outside\\secret.py"
        submission["cases"][0]["claims"].append(
            {
                "text": "Private evidence.",
                "status": "exists",
                "citations": [{"path": secret_path, "symbol": "secret"}],
            }
        )
        report = research.evaluate(submission)
        encoded = json.dumps(report)
        self.assertNotIn("outside", encoded)
        self.assertNotIn("secret.py", encoded)
        self.assertIn("<unsafe-path>", encoded)

    def test_missing_case_and_partial_evidence_are_reported(self) -> None:
        submission = _good_submission()
        submission["cases"].pop()
        submission["cases"][0]["claims"].pop()
        report = research.evaluate(submission)
        partial = report["cases"][0]
        absent = report["cases"][-1]
        self.assertEqual(1, len(partial["missing_coverage"]))
        self.assertFalse(absent["present"])
        self.assertEqual(2, len(absent["missing_coverage"]))

    def test_three_canonical_identical_tool_calls_are_a_loop(self) -> None:
        submission = _good_submission()
        submission["cases"][2]["tool_calls"] = [
            {"tool": "search", "arguments": {"query": "promotion", "page": 1}},
            {"tool": "search", "arguments": {"page": 1, "query": "promotion"}},
            {"tool": "search", "arguments": {"query": "promotion", "page": 1}},
        ]
        case = research.evaluate(submission)["cases"][2]
        self.assertTrue(case["repeated_identical_tool_loop"])
        self.assertEqual(3, case["max_identical_tool_run"])
        self.assertNotIn("repeated_tool", case)

    def test_equivalent_integral_json_numbers_cannot_evade_tool_loop(self) -> None:
        submission = _good_submission()
        submission["cases"][2]["tool_calls"] = [
            {"tool": "search", "arguments": {"page": 1}},
            {"tool": "search", "arguments": {"page": 1.0}},
            {"tool": "search", "arguments": {"page": -0.0 + 1.0}},
        ]
        case = research.evaluate(submission)["cases"][2]
        self.assertTrue(case["repeated_identical_tool_loop"])
        self.assertEqual(3, case["max_identical_tool_run"])

    def test_programmatic_tool_arguments_must_be_strict_nested_json(self) -> None:
        submission = _good_submission()
        submission["cases"][0]["tool_calls"] = [
            {"tool": "search", "arguments": {"nested": ("not", "json")}}
        ]
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "JSON value"):
            research.evaluate(submission)

    def test_interleaved_calls_are_not_a_consecutive_loop(self) -> None:
        submission = _good_submission()
        submission["cases"][2]["tool_calls"] = [
            {"tool": "search", "arguments": {"query": "promotion"}},
            {"tool": "open", "arguments": {"path": "promotion_eval.py"}},
            {"tool": "search", "arguments": {"query": "promotion"}},
        ]
        case = research.evaluate(submission)["cases"][2]
        self.assertFalse(case["repeated_identical_tool_loop"])

    def test_unknown_duplicate_and_oversized_inputs_are_rejected(self) -> None:
        unknown = _good_submission()
        private_id = "private-customer-alpha"
        unknown["cases"][0]["id"] = private_id
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "unknown case") as caught:
            research.evaluate(unknown)
        self.assertNotIn(private_id, str(caught.exception))
        self.assertIn(research._opaque_evidence_id(private_id), str(caught.exception))

        duplicate = _good_submission()
        duplicate["cases"].append(duplicate["cases"][0])
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "duplicate case"):
            research.evaluate(duplicate)

        oversized = _good_submission()
        oversized["cases"][0]["answer"] = "x" * (research.MAX_TEXT_CHARS + 1)
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "exceeds"):
            research.evaluate(oversized)

    def test_required_fields_and_tool_names_are_strict(self) -> None:
        unknown = _good_submission()
        private_field = "private-customer-prose"
        unknown["cases"][0][private_field] = "not copied"
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "unknown field") as caught:
            research.evaluate(unknown)
        self.assertNotIn(private_field, str(caught.exception))

        missing = _good_submission()
        del missing["cases"][0]["claims"]
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "missing fields"):
            research.evaluate(missing)

        missing_arguments = _good_submission()
        missing_arguments["cases"][0]["tool_calls"] = [{"tool": "search"}]
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "missing fields"):
            research.evaluate(missing_arguments)

        unsafe_tool = _good_submission()
        unsafe_tool["cases"][0]["tool_calls"] = [
            {"tool": "private tool name", "arguments": {}}
        ]
        with self.assertRaisesRegex(research.ResearchBenchmarkError, "safe name"):
            research.evaluate(unsafe_tool)

    def test_markdown_contains_metrics_but_not_candidate_prose(self) -> None:
        submission = _good_submission()
        private_prose = "private sentinel content must not be copied"
        submission["cases"][0]["answer"] = private_prose
        report = research.evaluate(submission)
        markdown = research.render_markdown(report)
        self.assertIn("False negatives", markdown)
        self.assertNotIn(private_prose, markdown)

    def test_cli_writes_bounded_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            submission = root / "submission.json"
            json_output = root / "report.json"
            markdown = root / "report.md"
            submission.write_text(json.dumps(_good_submission()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "benchmark_repository_research.py"),
                    "--submission",
                    str(submission),
                    "--json",
                    str(json_output),
                    "--markdown",
                    str(markdown),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(research.REPORT_SCHEMA, json.loads(json_output.read_text())["schema"])
            self.assertIn("Repository research benchmark", markdown.read_text(encoding="utf-8"))

    def test_atomic_write_failure_preserves_destination_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "report.json"
            destination.write_text("original", encoding="utf-8")
            with mock.patch.object(research.os, "replace", side_effect=OSError("fail")):
                with self.assertRaises(OSError):
                    research._atomic_write(destination, "replacement")
            self.assertEqual("original", destination.read_text(encoding="utf-8"))
            self.assertEqual([destination], list(Path(temporary).iterdir()))

    def test_cli_prints_public_suite_without_submission(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "benchmark_repository_research.py"),
                "--print-suite",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        suite = json.loads(completed.stdout)
        self.assertEqual(research.SUITE_NAME, suite["name"])
        self.assertNotIn("Users", completed.stdout)

    def test_cli_refuses_input_output_collision_and_huge_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            submission = Path(temporary) / "submission.json"
            submission.write_text(json.dumps(_good_submission()), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "2"):
                research.main(
                    ["--submission", str(submission), "--json", str(submission)]
                )

            hardlink = Path(temporary) / "submission-hardlink.json"
            os.link(submission, hardlink)
            with self.assertRaisesRegex(
                research.ResearchBenchmarkError, "must be distinct"
            ):
                research._distinct_paths((submission, hardlink))

            huge = Path(temporary) / "huge.json"
            huge.write_bytes(b" " * (research.MAX_INPUT_BYTES + 1))
            with self.assertRaises(research.ResearchBenchmarkError):
                research._load_json(huge)

            nonfinite = Path(temporary) / "nonfinite.json"
            nonfinite.write_text('{"value": NaN}', encoding="utf-8")
            with self.assertRaises(research.ResearchBenchmarkError):
                research._load_json(nonfinite)


if __name__ == "__main__":
    unittest.main()
