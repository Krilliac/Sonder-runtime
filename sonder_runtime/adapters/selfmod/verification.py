"""Command recording, smoke probes, parent/oracle grading, and acceptance review."""
from __future__ import annotations

import hashlib
import json
import keyword
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager

from sonder_runtime.adapters.selfmod.backup_restore import sha256_file
from sonder_runtime.adapters.selfmod.workspace import is_protected, test_inventory
from sonder_runtime.application.evaluation import integrity as _integrity
from sonder_runtime.application.selfmod.candidate_isolation import (
    ISOLATION_KINDS,
    IsolationAttestation,
    IsolationAttestationError,
)
from sonder_runtime.application.selfmod.independent_oracle import (
    ORACLE_PROBE_KIND,
    OracleError,
    OracleReceipt,
    grade_frame,
    ledger_command as oracle_ledger_command,
    ledger_output as oracle_ledger_output,
    payload_nonce,
)


UNATTENDED_REQUIRED_KINDS = (
    "syntax", "regression", "regression_heavy", "held_out",
    "host_probe", "host_grade", "oracle_probe", "oracle_grade",
)

SMOKE_RECEIPT_PREFIX = "SELFMOD-SMOKE-RECEIPT"

SMOKE_PROBE = r"""
import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import sys

sys.path.insert(0, os.getcwd())
plan = json.loads(sys.argv[1])
root = pathlib.Path(os.getcwd()).resolve()
digest = hashlib.sha256()
failures = []

for index, rel in enumerate(plan["present"]):
    name = plan["names"].get(rel)
    try:
        if name:
            module = importlib.import_module(name)
        else:
            loader = importlib.util.spec_from_file_location(
                "_selfmod_smoke_%d" % index, root / rel
            )
            module = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(module)
    except BaseException as exc:
        failures.append("%s: %s: %s" % (rel, type(exc).__name__, exc))
        continue
    source = getattr(module, "__file__", None)
    if not source:
        failures.append("%s: imported but reports no __file__" % rel)
        continue
    source = pathlib.Path(source).resolve()
    if not source.is_relative_to(root):
        failures.append(
            "%s: import resolved outside the candidate workspace: %s" % (rel, source)
        )
        continue
    digest.update(b"import\0" + rel.encode("utf-8") + b"\0")
    digest.update(hashlib.sha256(source.read_bytes()).hexdigest().encode("ascii") + b"\n")

for rel in plan["absent"]:
    name = plan["names"].get(rel)
    if (root / rel).exists():
        failures.append("%s: declared deleted but still present" % rel)
        continue
    try:
        resolved = importlib.util.find_spec(name) if name else None
    except BaseException:
        resolved = None
    if resolved is not None:
        failures.append("%s: deleted module still resolves as %r" % (rel, name))
        continue
    digest.update(b"gone\0" + rel.encode("utf-8") + b"\n")

if failures:
    sys.stdout.write("SELFMOD SMOKE FAILED -- the candidate does not run:\n")
    for line in failures:
        sys.stdout.write("  %s\n" % line)
    raise SystemExit(1)

sys.stdout.write(
    "SELFMOD-SMOKE-RECEIPT %s modules=%d gone=%d\n"
    % (digest.hexdigest(), len(plan["present"]), len(plan["absent"]))
)
"""


def _encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def auto_candidate(run: Mapping[str, Any]) -> bool:
    """Whether ``run`` is eligible for unattended low-risk evaluation."""
    return (
        run.get("mode") == "auto-low-risk"
        and run.get("risk") == "low"
        and not run.get("approval_required", True)
    )


def module_name(rel: str) -> str | None:
    """Dotted import name for a repo-relative ``.py`` path, or ``None``."""
    parts = tuple(PurePosixPath(rel).parts)
    if not parts or not parts[-1].endswith(".py"):
        return None
    parts = parts[:-1] if parts[-1] == "__init__.py" else parts[:-1] + (parts[-1][:-3],)
    if not parts:
        return None
    if not all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
        return None
    return ".".join(parts)


def smoke_plan(
    run_id: str,
    *,
    get_run_fn: Callable[[str], dict[str, Any]],
    candidate_path_fn: Callable[[str], Path],
) -> dict[str, Any]:
    """Split the run's declared Python surface into must-import and must-be-gone."""
    run = get_run_fn(run_id)
    workspace = candidate_path_fn(run_id)
    declared = sorted(path for path in run["files"] if path.lower().endswith(".py"))
    present = [path for path in declared if (workspace / path).is_file()]
    absent = [path for path in declared if not (workspace / path).is_file()]
    names = {path: module_name(path) for path in declared}
    return {
        "workspace": workspace,
        "declared": declared,
        "present": present,
        "absent": absent,
        "names": {key: value for key, value in names.items() if value},
    }


def smoke_receipt(
    workspace: Path, present: Sequence[str], absent: Sequence[str]
) -> str:
    """Compute the expected parent-side smoke receipt from candidate bytes on disk."""
    digest = hashlib.sha256()
    for rel in present:
        digest.update(b"import\0" + rel.encode("utf-8") + b"\0")
        digest.update(sha256_file(workspace / rel).encode("ascii") + b"\n")
    for rel in absent:
        digest.update(b"gone\0" + rel.encode("utf-8") + b"\n")
    return "%s %s modules=%d gone=%d" % (
        SMOKE_RECEIPT_PREFIX, digest.hexdigest(), len(present), len(absent),
    )


def record_smoke(
    run_id: str,
    *,
    timeout: int | None = None,
    protected_paths: Sequence[str | Path] = (),
    low_integrity: bool | None = None,
    isolation: Mapping[str, Any] | None = None,
    smoke_probe_script: str = SMOKE_PROBE,
    get_run_fn: Callable[[str], dict[str, Any]],
    smoke_plan_fn: Callable[[str], dict[str, Any]],
    record_command_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Run the candidate smoke probe and require the parent-computed receipt."""
    run = get_run_fn(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("tests may run only in testing phase")
    plan = smoke_plan_fn(run_id)
    seconds = min(
        int(timeout or run["budgets"]["max_test_seconds"]),
        int(run["budgets"]["max_test_seconds"]),
    )
    workspace = Path(plan["workspace"])
    if not plan["declared"]:
        message = (
            "selfmod smoke gate: this run declares no Python file (%s), so there is "
            "nothing for a smoke check to execute. An empty target set is a refusal, "
            "not a pass -- declare the module the change actually affects."
            % (", ".join(run["files"]) or "no files")
        )
        return record_command_fn(
            run,
            "smoke",
            [sys.executable, "-c", "raise SystemExit(%r)" % message],
            workspace,
            seconds,
            protected_paths=protected_paths,
            low_integrity=low_integrity,
            isolation=isolation,
        )
    payload = _encode_json({
        "present": plan["present"],
        "absent": plan["absent"],
        "names": plan["names"],
    })
    command = [sys.executable, "-c", smoke_probe_script, payload]
    receipt = smoke_receipt(workspace, plan["present"], plan["absent"])
    return record_command_fn(
        run,
        "smoke",
        command,
        workspace,
        seconds,
        receipt=receipt,
        protected_paths=protected_paths,
        low_integrity=low_integrity,
        isolation=isolation,
    )


@dataclass(frozen=True)
class RecordCommandHooks:
    """Injectable host callbacks for :func:`record_command`."""

    candidate_supervisor: Callable[[], tuple[Callable[..., dict[str, Any]], str]]
    linux_isolation_unconfigured: Callable[[], str]
    run_cmd: Callable[[Sequence[str], Path, int], tuple[int, str, int]]
    tx: Callable[..., ContextManager[sqlite3.Connection]]
    event: Callable[[sqlite3.Connection, str | None, str, str], None]


def record_command(
    run: Mapping[str, Any],
    kind: str,
    command: Sequence[str],
    cwd_path: Path,
    seconds: int,
    *,
    expect_failure: bool = False,
    receipt: str | None = None,
    protected_paths: Sequence[str | Path] = (),
    low_integrity: bool | None = None,
    isolation: Mapping[str, Any] | None = None,
    hooks: RecordCommandHooks,
) -> dict[str, Any]:
    """Run one candidate or baseline check with isolation attestation and write-watching."""
    run_id = str(run["id"])
    isolation_failed = False
    attestation = "unverified"
    typed_attestation: IsolationAttestation | None = None
    is_auto = auto_candidate(run) and kind != "reproducer_before"
    if is_auto and low_integrity is False:
        raise PermissionError(
            "auto-low-risk candidate tests require low-integrity isolation"
        )
    use_low_integrity = is_auto or (
        os.environ.get("SELFMOD_LOW_INTEGRITY") == "1"
        if low_integrity is None
        else bool(low_integrity)
    )
    watch = (
        None
        if kind == "reproducer_before"
        else _integrity.ProtectedWriteWatch.start(run.get("workspace_path") or cwd_path)
    )
    if use_low_integrity:
        started = time.monotonic()
        expected = "low"
        try:
            run_isolated, expected = hooks.candidate_supervisor()
            isolated = run_isolated(
                command,
                cwd=cwd_path,
                timeout=seconds,
                protected_paths=protected_paths,
                **dict(isolation or {}),
            )
            code = int(isolated["exit_code"])
            output = str(isolated.get("output") or "")
            job = isolated.get("job")
            geteuid_fn: Callable[[], int] | None = getattr(os, "geteuid", None)
            supervisor_uid = geteuid_fn() if geteuid_fn is not None else None
            try:
                typed = IsolationAttestation.from_supervisor_result(
                    isolated,
                    expected_kind=expected,
                    supervisor_uid=supervisor_uid,
                )
            except IsolationAttestationError:
                raise RuntimeError("invalid supervisor attestation") from None
            supplied = isolated.get("attestation")
            if supplied is not None and supplied != typed:
                raise RuntimeError("invalid supervisor attestation")
            if typed.integrity_failed:
                raise RuntimeError("evaluator integrity failed")
            attestation = expected
            typed_attestation = typed
            output = (output + "\nSELFMOD ISOLATION: %s\n" % _encode_json(job))[-100_000:]
        except Exception as exc:
            code = 125
            known = type(exc) is RuntimeError and str(exc) in {
                "invalid supervisor attestation",
                "evaluator integrity failed",
            }
            if expected == "linux-uid":
                reason = (
                    str(exc)[:300]
                    if isinstance(exc, RuntimeError)
                    else type(exc).__name__
                )
            elif os.name != "nt":
                reason = "unsupported platform"
                if sys.platform.startswith("linux"):
                    reason += "; %s" % hooks.linux_isolation_unconfigured()
            else:
                reason = str(exc) if known else type(exc).__name__
            output = "%s isolation unavailable: %s" % (
                "linux-uid" if expected == "linux-uid" else "low-integrity",
                reason,
            )
            isolation_failed = True
        duration = int((time.monotonic() - started) * 1000)
    else:
        code, output, duration = hooks.run_cmd(command, cwd_path, seconds)
    passed = (
        code not in (0, 124, 125) if expect_failure else code == 0
    ) and not isolation_failed
    if receipt is not None and passed and receipt not in output:
        passed = False
        output = (
            "%s\nSELFMOD %s GATE REFUSED: the probe exited %s but did not report the "
            "receipt it could only produce by examining the candidate.\n"
            "  expected: %s"
            % (output, str(kind).upper(), code, receipt)
        )[:100_000]
    writes = watch.finish() if watch is not None else ()
    if writes:
        passed, output = (
            False,
            (output + _integrity.protected_write_note(kind, writes))[-100_000:],
        )
    stored_command: list[str] = list(command)
    stored_output = output
    if kind == ORACLE_PROBE_KIND:
        stored_command = oracle_ledger_command(command)
        stored_output = oracle_ledger_output(output)
    with hooks.tx() as conn:
        cursor = conn.execute(
            "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts,isolation) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                str(kind)[:80],
                _encode_json(stored_command),
                code,
                duration,
                stored_output,
                int(passed),
                time.time(),
                attestation,
            ),
        )
        test_id = getattr(cursor, "lastrowid", None)
        if writes:
            sql, params = _integrity.protected_write_row(
                run_id, test_id, kind, writes, attestation, time.time()
            )
            conn.execute(sql, params)
        hooks.event(
            conn,
            run_id,
            "test",
            "%s exit=%s expected=%s duration_ms=%s"
            % (kind, code, "failure" if expect_failure else "success", duration),
        )
    return {
        "kind": kind,
        "command": list(command),
        "exit_code": code,
        "duration_ms": duration,
        "output": output,
        "passed": passed,
        "test_id": test_id,
        "isolation": attestation,
        "attestation": typed_attestation,
    }


def record_host_grade(
    run_id: str,
    probe_id: int,
    *,
    passed: bool,
    detail: str,
    get_run_fn: Callable[[str], dict[str, Any]],
    tx_fn: Callable[..., ContextManager[sqlite3.Connection]],
    event_fn: Callable[[sqlite3.Connection, str | None, str, str], None],
) -> dict[str, Any]:
    """Persist a parent-owned verdict bound to a successful isolated probe."""
    run = get_run_fn(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("host grade requires testing phase")
    with tx_fn() as conn:
        probe = conn.execute(
            "SELECT kind,passed,isolation FROM selfmod_tests WHERE run_id=? AND id=?",
            (run_id, probe_id),
        ).fetchone()
        if (
            probe is None
            or probe["kind"] != "host_probe"
            or not probe["passed"]
            or probe["isolation"] not in ISOLATION_KINDS
        ):
            raise PermissionError("host grade requires a passing attested probe")
        prior = conn.execute(
            "SELECT id FROM selfmod_tests WHERE run_id=? AND kind='host_grade'",
            (run_id,),
        ).fetchone()
        if prior is not None:
            raise RuntimeError("host grade was already recorded")
        conn.execute(
            "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts,isolation) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "host_grade",
                _encode_json({"probe_id": probe_id}),
                0 if passed else 1,
                0,
                str(detail)[:100_000],
                int(bool(passed)),
                time.time(),
                probe["isolation"],
            ),
        )
        event_fn(
            conn,
            run_id,
            "host_grade",
            "parent-scored challenge %s" % ("passed" if passed else "failed"),
        )
    return {"kind": "host_grade", "passed": bool(passed), "detail": str(detail)[:100_000]}


@dataclass(frozen=True)
class OracleGradeHooks:
    """Injectable host callbacks for :func:`record_oracle_grade`."""

    get_run: Callable[[str], dict[str, Any]]
    candidate_snapshot: Callable[[str], dict[str, Any]]
    tested_digests: Callable[[str], dict[str, Any] | None]
    baseline_binding: Callable[[Mapping[str, Any]], dict[str, str]]
    current_case_digest: Callable[[Path], str]
    tx: Callable[..., ContextManager[sqlite3.Connection]]
    event: Callable[[sqlite3.Connection, str | None, str, str], None]


def record_oracle_grade(
    run_id: str,
    probe: Any,
    *,
    challenge: Any,
    loaded: Any,
    attestation: Any,
    confidential: bool,
    read_denied: bool,
    hooks: OracleGradeHooks,
) -> dict[str, Any]:
    """Grade an oracle probe in the evaluator process and record its receipt."""
    run = hooks.get_run(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("oracle grade requires testing phase")
    probe_id = probe.get("test_id") if isinstance(probe, dict) else None
    command, output = (
        (probe.get("command"), probe.get("output"))
        if isinstance(probe, dict)
        else (None, None)
    )
    if type(probe_id) is not int or not isinstance(command, list) or not isinstance(output, str):
        raise PermissionError("oracle grade requires the recorded oracle probe result")
    candidate = hooks.candidate_snapshot(run_id)
    if hooks.tested_digests(run_id) != candidate:
        raise RuntimeError("oracle grade requires the tested candidate bytes")
    baseline = hooks.baseline_binding(run)
    if hooks.current_case_digest(loaded.path) != loaded.sha256:
        raise RuntimeError("held oracle case set changed after it was loaded")
    typed = attestation if isinstance(attestation, IsolationAttestation) else None
    with hooks.tx() as conn:
        row = conn.execute(
            "SELECT kind,passed,isolation,output,command_json FROM selfmod_tests WHERE run_id=? AND id=?",
            (run_id, probe_id),
        ).fetchone()
        if (
            row is None
            or row["kind"] != ORACLE_PROBE_KIND
            or row["isolation"] not in ISOLATION_KINDS
            or typed is None
            or typed.kind != row["isolation"]
        ):
            raise PermissionError("oracle grade requires an attested oracle probe")
        if (
            row["command_json"] != _encode_json(oracle_ledger_command(command))
            or row["output"] != oracle_ledger_output(output)
            or bool(row["passed"]) is not bool(probe.get("passed"))
        ):
            raise PermissionError("oracle probe result does not match its ledger row")
        if payload_nonce(command) != challenge.nonce:
            raise PermissionError("oracle probe row was not issued for this challenge")
        if (
            conn.execute(
                "SELECT 1 FROM selfmod_oracle_receipts WHERE run_id=?", (run_id,)
            ).fetchone()
            is not None
        ):
            raise RuntimeError("oracle grade was already recorded")
        probe_passed = bool(row["passed"])
        verdict = grade_frame(output if probe_passed else "", challenge, loaded.case_set)
        detail = verdict.detail if probe_passed else "oracle probe did not exit cleanly"
        receipt = OracleReceipt(
            run_id=run_id,
            probe_id=probe_id,
            attestation=typed.kind,
            candidate_uid=typed.candidate_uid,
            supervisor_uid=typed.supervisor_uid,
            case_set_sha256=loaded.sha256,
            case_count=verdict.case_count,
            matched=verdict.matched,
            nonce=challenge.nonce,
            outputs_sha256=verdict.outputs_sha256 if probe_passed else None,
            confidential=bool(confidential),
            read_denied=bool(confidential and read_denied),
            passed=bool(verdict.passed and probe_passed),
            candidate=candidate,
            baseline=baseline,
        )
        text = receipt.to_json()
        conn.execute(
            "INSERT INTO selfmod_oracle_receipts VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                probe_id,
                int(receipt.passed),
                int(receipt.independent),
                text,
                receipt.digest(),
                time.time(),
            ),
        )
        summary = "%s; independent=%s; receipt sha256=%s" % (
            detail, "yes" if receipt.independent else "no", receipt.digest()
        )
        conn.execute(
            "INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts,isolation) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "oracle_grade",
                _encode_json({"probe_id": probe_id}),
                0 if receipt.passed else 1,
                0,
                summary,
                int(receipt.passed),
                time.time(),
                row["isolation"],
            ),
        )
        hooks.event(
            conn,
            run_id,
            "oracle_grade",
            "independent oracle %s (independent=%s)"
            % ("passed" if receipt.passed else "failed", receipt.independent),
        )
    return {
        "kind": "oracle_grade",
        "passed": receipt.passed,
        "independent": receipt.independent,
        "detail": detail,
        "receipt_sha256": receipt.digest(),
    }


def load_oracle_receipt(
    run_id: str, *, connect_fn: Callable[[], sqlite3.Connection]
) -> OracleReceipt | None:
    """Load and self-consistency-verify a run's durable oracle receipt."""
    with connect_fn() as conn:
        row = conn.execute(
            "SELECT passed,independent,receipt_json,receipt_sha256 FROM selfmod_oracle_receipts WHERE run_id=?",
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    receipt = OracleReceipt.from_json(row["receipt_json"])
    if (
        receipt.digest() != row["receipt_sha256"]
        or receipt.run_id != run_id
        or bool(row["passed"]) is not receipt.passed
        or bool(row["independent"]) is not receipt.independent
    ):
        raise OracleError("oracle receipt does not match its recorded digest")
    return receipt


def oracle_admission_refusal(
    run_id: str,
    run: Mapping[str, Any],
    *,
    oracle_receipt_fn: Callable[[str], OracleReceipt | None],
    tested_digests_fn: Callable[[str], dict[str, Any] | None],
    candidate_snapshot_fn: Callable[[str], dict[str, Any]],
    baseline_binding_fn: Callable[[Mapping[str, Any]], dict[str, str]],
) -> str | None:
    """Return why the independent oracle does not authorize unattended promotion, else None."""
    try:
        receipt = oracle_receipt_fn(run_id)
        if receipt is None:
            return "no independent oracle receipt"
        tested = tested_digests_fn(run_id)
        if tested is None or tested != candidate_snapshot_fn(run_id):
            return "candidate bytes changed after testing began"
        return receipt.admission_refusal(
            candidate=tested, baseline=baseline_binding_fn(run)
        )
    except (OracleError, RuntimeError, OSError, ValueError) as exc:
        return "oracle receipt could not be verified (%s)" % type(exc).__name__


@dataclass(frozen=True)
class ReviewHooks:
    """Injectable host callbacks for :func:`review_run`."""

    get_run: Callable[[str], dict[str, Any]]
    inspect_diff: Callable[[str], dict[str, Any]]
    test_results: Callable[[str], list[dict[str, Any]]]
    tested_digests: Callable[[str], dict[str, Any] | None]
    candidate_snapshot: Callable[[str], dict[str, Any]]
    verify_backup: Callable[[str], dict[str, Any]]
    candidate_path: Callable[[str], Path]
    backup_rehearsal: Callable[[str], bool]
    oracle_admission_refusal: Callable[[str, Mapping[str, Any]], str | None]
    phase: Callable[..., dict[str, Any]]
    restore: Callable[..., dict[str, Any]]
    approve: Callable[..., dict[str, Any]]


def review_run(
    run_id: str,
    *,
    require_kinds: Iterable[str] | None = None,
    unevaluated: Iterable[str] = (),
    hooks: ReviewHooks,
) -> dict[str, Any]:
    """Deterministic acceptance review for a tested selfmod candidate."""
    unevaluated_items = tuple(str(item)[:300] for item in (unevaluated or ()))
    run = hooks.get_run(run_id)
    if run["phase"] != "testing":
        raise RuntimeError("review requires testing phase")
    diff = hooks.inspect_diff(run_id)
    results = hooks.test_results(run_id)
    failures: list[str] = []
    required = set(
        require_kinds
        or ("reproducer_before", "syntax", "targeted", "regression", "smoke")
    )
    if run["maintenance_authorized"]:
        required.add("security")
    seen = {row["kind"] for row in results if row["passed"]}
    if required - seen:
        failures.append("missing passing checks: %s" % ", ".join(sorted(required - seen)))
    if any(not row["passed"] for row in results):
        failures.append("one or more recorded checks failed")
    failures.extend(_integrity.review_refusals(results))
    if "reproducer_before" in required:
        baseline = [row for row in results if row["kind"] == "reproducer_before"]
        if not baseline or not any(
            row["exit_code"] not in (None, 0) and row["passed"] for row in baseline
        ):
            failures.append("original failure was not demonstrated before editing")
    if not diff["changed_files"]:
        failures.append("candidate produced no scoped diff")
    tested = hooks.tested_digests(run_id)
    if tested is None:
        failures.append("no tested-bytes record")
    elif tested != hooks.candidate_snapshot(run_id):
        failures.append("candidate bytes changed after testing began")
    if any(is_protected(path) for path in diff["changed_files"]) and not run["maintenance_authorized"]:
        failures.append("protected file modified")
    manifest = hooks.verify_backup(run_id)
    existing_tests = {
        record["path"]
        for record in manifest["files"]
        if record["existed_before"]
        and (
            record["path"].startswith("tests/")
            or Path(record["path"]).name.startswith("test_")
        )
    }
    weakened_surface = existing_tests & set(diff["changed_files"])
    if weakened_surface:
        failures.append(
            "pre-existing required tests were modified: %s"
            % ", ".join(sorted(weakened_surface))
        )
    after_inventory = test_inventory(hooks.candidate_path(run_id))
    before_inventory = set(run["test_inventory_before"])
    if not before_inventory.issubset(set(after_inventory)):
        failures.append("test inventory was weakened")
    if not hooks.backup_rehearsal(run_id):
        failures.append("rollback rehearsal failed")
    candidate_results = [row for row in results if row["kind"] != "reproducer_before"]
    host_grades = [row for row in candidate_results if row["kind"] == "host_grade"]
    oracle_grades = [row for row in candidate_results if row["kind"] == "oracle_grade"]
    if any(not row["passed"] for row in oracle_grades):
        failures.append("independent oracle failed")
    oracle_refusal = hooks.oracle_admission_refusal(run_id, run)
    unattended_missing = sorted(
        set(UNATTENDED_REQUIRED_KINDS)
        - {row["kind"] for row in candidate_results if row["passed"]}
    )
    auto_eligible = bool(
        oracle_refusal is None
        and len(oracle_grades) == 1
        and not unattended_missing
        and not failures
        and not unevaluated_items
        and candidate_results
        and run["mode"] == "auto-low-risk"
        and run["risk"] == "low"
        and not run["approval_required"]
        and len(host_grades) == 1
        and host_grades[0]["passed"]
        and all(row["isolation"] in ISOLATION_KINDS for row in candidate_results)
    )
    target = "rejected" if failures else "reviewing"
    passed_note = "deterministic acceptance checks passed"
    if unevaluated_items:
        passed_note += (
            "; NOT EVALUATED (human review required): " + "; ".join(unevaluated_items)
        )
    elif run["mode"] == "auto-low-risk" and not auto_eligible and not failures:
        passed_note += (
            "; independent evaluator authority unverified (%s; human review required)"
            % (
                oracle_refusal
                or (
                    "unattended gates not passed: %s" % ", ".join(unattended_missing)
                    if unattended_missing
                    else "an existing unattended gate is unmet"
                )
            )
        )
    hooks.phase(
        run_id,
        {"testing"},
        target,
        "review",
        "; ".join(failures) if failures else passed_note[:1000],
        test_inventory_after=_encode_json(after_inventory),
        last_error="; ".join(failures),
        auto_evaluation_eligible=int(auto_eligible),
    )
    if failures:
        hooks.restore(run_id, from_candidate_only=True)
    elif auto_eligible:
        hooks.approve(run_id, approver="host:auto-low-risk")
    return hooks.get_run(run_id)
