"""Security contracts for the app-safe activity and execution-feed projections.

record_tool_result stores tool stdout and summaries through it; public_snapshot
then applies an independent bounded projection before HTTP clients receive it.
It had drifted behind npu_contract's rule set, which does the same job on the
same shapes; these pin the shapes that were measured passing through it.
"""
import json

import activity_tracker as at


def test_aws_style_env_names_are_redacted():
    # '_' is a word character, so the old \bsecret\b could not match inside
    # AWS_SECRET_ACCESS_KEY -- the most standard secret env var there is.
    out = at._redact_text("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCY")
    assert "wJalr" not in out
    assert "<redacted>" in out
    assert "CLIENT_SECRET=hunter2" not in at._redact_text("CLIENT_SECRET=hunter2")


def test_quoted_value_is_redacted_whole():
    # The old value pattern stopped at the first space, so this came back as
    # `token=<redacted> with spaces"` -- leaking while looking sanitized.
    out = at._redact_text('token = "value with spaces"')
    assert "value with spaces" not in out
    assert "spaces" not in out


def test_aws_access_key_id_shape_is_redacted():
    out = at._redact_text("using AKIAIOSFODNN7EXAMPLE for upload")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "for upload" in out


def test_uri_embedded_credentials_are_redacted():
    out = at._redact_text("connect postgres://admin:S3cr3tPw@db.internal:5432/prod")
    assert "S3cr3tPw" not in out
    assert "db.internal" in out  # host stays: the ledger is still diagnostic


def test_bare_authorization_header_is_redacted():
    out = at._redact_text("Authorization: abcdef0123456789abcdef")
    assert "abcdef0123456789abcdef" not in out


def test_bearer_token_survives_the_authorization_keyword():
    # _SECRET_ASSIGNMENT_RE matches "Authorization:" and stops at the space, so
    # if it ran before _BEARER_RE it would consume the word "Bearer" and strand
    # the token itself. Order is load-bearing.
    out = at._redact_text("Authorization: Bearer abc.def.ghi-0123456789")
    assert "abc.def.ghi-0123456789" not in out


def test_jwt_is_redacted_but_dotted_identifiers_are_not():
    jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
    assert jwt not in at._redact_text("token issued: " + jwt)
    # a pytest nodeid / dotted module path is not a credential
    keep = "tests.test_activity_redaction.test_something_long"
    assert at._redact_text(keep) == keep


def test_redaction_does_not_run_past_the_end_of_a_line():
    # The separator is [ \t] only: a bare `pwd` cannot swallow the next line.
    out = at._redact_text("pwd\n/home/user/project")
    assert "/home/user/project" in out


def test_unrelated_flags_after_a_secret_are_kept():
    # Narrower than npu_contract on purpose -- this ledger is read to see what
    # actually ran, so an unquoted value ends at whitespace, not end of line.
    out = at._redact_text("deploy --token abcd1234 --verbose --region us-east-1")
    assert "abcd1234" not in out
    assert "--verbose" in out and "us-east-1" in out


def test_redactor_failure_fails_closed():
    class BrokenText:
        def __str__(self):
            raise RuntimeError("cannot render")

    assert at._redact_text(BrokenText()) == "<redaction-failed>"


def test_npu_events_remain_enum_only_even_with_authorized_detail():
    at.reset_for_tests()
    secret = r"C:\private\weights\model.onnx token=npu-secret"
    with at.response_span("npu-detail", "safe"):
        at.record_event(
            "npu_fallback",
            capability="embeddings",
            reason="ram_gate",
            operation_mode="execution",
            fallback_handler="ollama",
            handler_state="pending",
            model=secret,
            path=secret,
            summary=secret,
            args={"content": secret},
            command=secret,
            output=secret,
            request_preview={"state": "available", "text": secret},
            prompt_chars=123,
        )

    feed = at.execution_feed(include_detail=True)
    event = next(row for row in feed["events"] if row["kind"] == "npu_fallback")
    assert set(event) == {
        "response_id", "response_status", "seq", "ts", "elapsed_ms",
        "kind", "phase", "capability", "reason", "operation_mode",
        "fallback_handler", "handler_state",
    }
    assert secret not in json.dumps(feed)


def test_npu_enum_projection_fails_closed_when_value_cannot_render():
    class BrokenValue:
        def __str__(self):
            raise RuntimeError(r"C:\private\model token=secret")

    event = at._public_event({
        "kind": "npu_fallback", "capability": BrokenValue(),
        "reason": BrokenValue(), "operation_mode": BrokenValue(),
        "fallback_handler": BrokenValue(), "handler_state": BrokenValue(),
    }, include_detail=True)
    assert event["capability"] == "unknown"
    assert event["reason"] == "unknown"
    assert "private" not in json.dumps(event)


def test_public_activity_defaults_to_metadata_only_and_basename_paths(monkeypatch):
    monkeypatch.delenv("SONDER_EXECUTION_FEED_DETAIL", raising=False)
    at.reset_for_tests()
    with at.response_span("work", "private prose token=prompt-secret"):
        at.record_model_call(
            model="local-model", request_preview="token=request-secret",
            response_preview="password=response-secret", ok=True,
        )
        at.record_tool_result(
            "file_write",
            {"path": r"C:\private\report.txt", "content": "api_key=arg-secret"},
            command=["writer", "--token", "command-secret"],
            output="CLIENT_SECRET=output-secret",
        )
        at.record_file_change(
            "create", r"C:\private\report.txt", lines_added=2,
            preview="AWS_SECRET_ACCESS_KEY=file-secret", preview_kind="content",
        )

    public = at.public_snapshot()
    encoded = json.dumps(public)
    for secret in (
        "private prose", "prompt-secret", "request-secret", "response-secret",
        "arg-secret", "command-secret", "output-secret", "file-secret",
    ):
        assert secret not in encoded
    assert r"C:\private" not in encoded
    assert "report.txt" in encoded
    assert "prompt" not in public["latest"]
    assert public["detail_enabled"] is False
    model = next(row for row in public["latest"]["events"] if row["kind"] == "model_call")
    assert model["request_preview"]["state"] == "disabled"
    assert next(row for row in public["latest"]["events"] if row["kind"] == "response_start")["phase"] == "started"
    file_event = next(row for row in public["latest"]["events"] if row["kind"] == "file_change")
    assert file_event["preview"]["state"] == "disabled"
    assert file_event["phase"] == "applied"


def test_public_paths_never_disclose_relative_directory_components():
    cases = {
        r"private\customer-name\secret-project\foo.py": "foo.py",
        "private/customer-name/secret-project/foo.py": "foo.py",
        r"private/mixed\secret/foo.py": "foo.py",
        r"..\private\foo.py": "foo.py",
        r"C:private\foo.py": "foo.py",
        "foo.py": "foo.py",
    }
    for source, expected in cases.items():
        assert at._safe_path(source) == expected


def test_metadata_projection_suppresses_all_free_text_summaries(monkeypatch):
    monkeypatch.delenv("SONDER_EXECUTION_FEED_DETAIL", raising=False)
    at.reset_for_tests()
    with at.response_span("work", "private prompt"):
        at.record_event("notice", summary="SUMMARY_CANARY")
        at.record_file_change(
            "edit", "report.txt", summary="FILE_SUMMARY_CANARY",
        )
        at.set_checklist({
            "id": "check-1", "title": "CHECKLIST_TITLE_CANARY",
            "status": "running", "summary": "CHECKLIST_SUMMARY_CANARY",
            "items": [{
                "id": "item-1", "title": "CHECKLIST_ITEM_CANARY",
                "status": "pending",
            }],
        })
        at.set_result_summary("RESULT_CANARY")

    encoded = json.dumps(at.public_snapshot(include_detail=False))
    for canary in (
        "SUMMARY_CANARY", "FILE_SUMMARY_CANARY", "RESULT_CANARY",
        "CHECKLIST_TITLE_CANARY", "CHECKLIST_SUMMARY_CANARY",
        "CHECKLIST_ITEM_CANARY",
    ):
        assert canary not in encoded

    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    detailed = json.dumps(at.public_snapshot(include_detail=True))
    assert "SUMMARY_CANARY" in detailed
    assert "RESULT_CANARY" in detailed


def test_detailed_execution_feed_is_bounded_redacted_and_versioned(monkeypatch):
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    at.reset_for_tests()
    with at.response_span("work", "not exported from response span"):
        at.record_model_call(
            model="local-model",
            request_preview="token=request-secret " + "q" * 1500,
            response_preview="password=response-secret " + "a" * 1500,
            ok=True,
        )
        at.record_tool_result(
            "run_code",
            {"code": "api_key=arg-secret\n" + "x" * 1500, "token": "never"},
            output="CLIENT_SECRET=output-secret\n" + "y" * 1500,
        )
        at.record_file_change(
            "edit", r"C:\private\source.py", lines_added=2, lines_edited=1,
            lines_deleted=3, preview="token=file-secret\n" + "z" * 1500,
            preview_kind="diff",
        )

    feed = at.execution_feed(at.snapshot())
    encoded = json.dumps(feed)
    assert feed["schema_version"] == 1
    assert feed["runtime_id"].startswith("rt-")
    assert feed["known"] is True
    assert len(feed["events"]) <= 20
    assert feed["bytes"] <= 64 * 1024
    assert feed["truncated"] is True
    assert feed["redaction_applied"] is True
    for secret in ("request-secret", "response-secret", "arg-secret", "output-secret", "file-secret", '"never"'):
        assert secret not in encoded
    model = next(row for row in feed["events"] if row["kind"] == "model_call")
    assert model["phase"] == "completed"
    assert model["request_preview"]["state"] == "available"
    assert model["request_preview"]["truncated"] is True
    tool = next(row for row in feed["events"] if row["kind"] == "tool_call")
    assert tool["args_preview"]["redacted"] is True
    changed = next(row for row in feed["events"] if row["kind"] == "file_change")
    assert changed["path"] == "source.py"
    assert (changed["lines_added"], changed["lines_edited"], changed["lines_deleted"]) == (2, 1, 3)


def test_public_response_and_feed_enforce_event_and_byte_caps(monkeypatch):
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    at.reset_for_tests()
    with at.response_span("bounded", "private prompt"):
        for index in range(35):
            at.record_tool_result(
                "run_code", {"code": "x" * 2000, "index": index},
                output="y" * 3000,
            )

    public = at.public_snapshot()
    latest = public["latest"]
    assert len(latest["events"]) <= 20
    assert len(json.dumps(latest).encode("utf-8")) <= 64 * 1024
    assert latest["truncated"] is True
    feed = at.execution_feed(public)
    assert len(feed["events"]) <= 20
    assert feed["bytes"] <= 64 * 1024
    assert feed["truncated"] is True


def test_detail_gate_requires_exact_one(monkeypatch):
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "true")
    assert at.detail_enabled() is False
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    assert at.detail_enabled() is True


def test_feed_ring_retains_rapidly_completed_responses_between_polls(monkeypatch):
    monkeypatch.delenv("SONDER_EXECUTION_FEED_DETAIL", raising=False)
    at.reset_for_tests()
    response_ids = []
    for label in ("first", "second"):
        with at.response_span(label, "private") as response:
            response_ids.append(response["id"])
            at.record_tool_call("file_read", {"path": "%s.txt" % label})

    snap = at.snapshot()
    assert snap["latest"]["id"] == response_ids[1]
    feed = at.execution_feed(snap)
    seen = {row["response_id"] for row in feed["events"]}
    assert set(response_ids).issubset(seen)
    seqs = [row["seq"] for row in feed["events"]]
    assert seqs == sorted(set(seqs))
    assert feed["oldest_seq"] == 1
    assert feed["next_seq"] > max(seqs)
    assert feed["dropped_events"] == 0


def test_feed_reports_ring_drops_and_sequence_window(monkeypatch):
    monkeypatch.delenv("SONDER_EXECUTION_FEED_DETAIL", raising=False)
    at.reset_for_tests()
    with at.response_span("burst", "private"):
        for index in range(at.MAX_EVENT_RING + 20):
            at.record_event("heartbeat", summary="event %d" % index)

    feed = at.execution_feed(at.snapshot())
    assert feed["dropped_events"] > 0
    assert feed["sequence_gap"] == feed["dropped_events"]
    assert feed["oldest_seq"] > 1
    assert feed["truncated"] is True
