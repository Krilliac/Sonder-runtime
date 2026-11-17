"""Security contracts for the app-safe activity and execution-feed projections.

record_tool_result stores tool stdout and summaries through it; public_snapshot
then applies an independent bounded projection before HTTP clients receive it.
It had drifted behind npu_contract's rule set, which does the same job on the
same shapes; these pin the shapes that were measured passing through it.
"""
import json

import pytest

import sonder_runtime.adapters.observability.activity_tracker as at
import server


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


def test_program_prefixed_json_argv_masks_secret_pairs():
    """Agent activity renders command argv as ``program [..]``."""
    secret = "json-argv-secret"
    out = at._safe_command(
        'python ["build.py", "--token", "json-argv-secret", "--verbose"]'
    )

    assert secret not in out
    assert 'python ["build.py", "--token", "<redacted>", "--verbose"]' == out


def test_agent_activity_json_encoded_argv_masks_secret_pairs():
    """The server's args_json bridge must preserve argv structure for masking."""
    raw = server._agent_activity_command(
        "workspace_run",
        {"program": "python", "args_json": '["--token", "json-secret"]'},
    )

    assert raw == 'python ["--token", "json-secret"]'
    assert "json-secret" not in at._safe_command(raw)
    assert at._safe_command(raw) == 'python ["--token", "<redacted>"]'

    script_raw = server._agent_activity_command(
        "script_run",
        {"path": "build.py", "args_json": '["--password", "script-secret"]'},
    )
    assert script_raw == 'build.py ["--password", "script-secret"]'
    assert "script-secret" not in at._safe_command(script_raw)


def test_agent_activity_deep_json_argv_falls_back_without_raising():
    """Malformed/deep argv JSON must not abort activity recording."""
    deep = "[" * 1100 + "0" + "]" * 1100

    for tool_name, args in (
        ("workspace_run", {"program": "python", "args_json": deep}),
        ("script_run", {"path": "build.py", "args_json": deep}),
    ):
        raw = server._agent_activity_command(tool_name, args)
        assert raw.startswith(("python ", "build.py "))


def test_program_prefixed_json_argv_handles_a_bracket_in_the_program_path():
    secret = "bracketed-path-secret"
    out = at._safe_command(
        'tools/tool[debug ["--token", "bracketed-path-secret", "--verbose"]'
    )

    assert secret not in out
    assert out == 'tools/tool[debug ["--token", "<redacted>", "--verbose"]'


def test_program_prefixed_json_argv_keeps_non_json_text_as_text():
    value = "runner [not JSON --token still-redacted]"
    out = at._safe_command(value)

    assert "still-redacted" not in out
    assert "runner [not JSON" in out


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


def test_public_activity_keeps_scalar_fanout_metrics_without_content():
    at.reset_for_tests()
    with at.response_span("chat", "prompt-canary"):
        at.record_model_fanout(
            cloud_model_calls=4, tokens_in=321, tokens_out=123,
            answered=2, failed=1, unknown=1, skipped=1, elapsed_ms=456,
        )

    public = at.public_snapshot(include_detail=False)
    event = next(row for row in public["latest"]["events"] if row["kind"] == "model_fanout")
    assert {key: event[key] for key in (
        "cloud_model_calls", "tokens_in", "tokens_out", "answered", "failed", "unknown", "skipped",
    )} == {
        "cloud_model_calls": 4, "tokens_in": 321, "tokens_out": 123,
        "answered": 2, "failed": 1, "unknown": 1, "skipped": 1,
    }
    encoded = json.dumps(public)
    assert "prompt-canary" not in encoded
    assert "model" not in event
    assert "summary" not in event

    feed = at.execution_feed(at.snapshot(), include_detail=False)
    row = next(row for row in feed["events"] if row["kind"] == "model_fanout")
    assert row["cloud_model_calls"] == 4
    assert row["tokens_in"] == 321 and row["tokens_out"] == 123
    assert "cloud calls=4 answered=2 failed=1 unknown=1 skipped=1 tok=321/123" in at.format_execution_feed(feed)


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


def test_uri_credentials_are_redacted_after_mixed_scheme_prefixes():
    from sonder_runtime.adapters.observability import activity_tracker as at
    for text in (
        "clone https://user:pw@example.com/repo",
        "1-http://user:pw@example.com",
        "see git+ssh://user:pw@host/x",
    ):
        out = at._redact_text(text)
        assert "user:pw@" not in out, out
        assert "<redacted>@" in out, out


def test_uri_credential_redaction_is_linear_on_long_scheme_runs():
    import time
    from sonder_runtime.adapters.observability import activity_tracker as at
    for run in ("a-" * 20000, "a1" * 20000, "a." * 20000):
        started = time.perf_counter()
        at._redact_text(run)
        # Quadratic scanning took seconds here; a linear scan takes milliseconds.
        assert time.perf_counter() - started < 1.0


def test_redaction_is_linear_on_repeated_keywords():
    import time

    for payload in ("token" * 10_000, "password_" * 5_600, "secret=" * 7_200,
                    "api_key" * 7_200, "token " * 8_400):
        assert len(payload) >= 50_000
        started = time.perf_counter()
        at._redact_text(payload)
        assert time.perf_counter() - started < 0.5


def test_redaction_caps_input_and_never_returns_the_dropped_tail():
    tail = "AWS_SECRET_ACCESS_KEY=hunter2-not-real"
    out = at._redact_text("x" * (at.MAX_REDACT_CHARS + 10) + tail)
    assert len(out) < at.MAX_REDACT_CHARS + 100
    assert "hunter2" not in out


def test_redaction_output_unchanged_for_prefixed_names():
    assert at._redact_text("MY_DB_PASSWORD=hunter2-not-real ok") == "MY_DB_PASSWORD=<redacted> ok"


def test_record_tool_result_redacts_outside_the_lock(monkeypatch):
    held = []
    real = at._redact_text

    def spy(value):
        held.append(at._LOCK._is_owned())
        return real(value)

    monkeypatch.setattr(at, "_redact_text", spy)
    monkeypatch.setattr(at, "_current", lambda: {"tool_calls": 0, "events": []})
    monkeypatch.setattr(at, "_event", lambda *a, **k: None)
    at.record_tool_result("file_read", {"p": "x"}, command="c", output="o", summary="s")
    assert held and not any(held)


def test_long_key_name_after_keyword_is_still_redacted():
    name = "my_service_access_key" + "_" + "x" * 100
    out = at._redact_text(name + "=hunter2-not-real")
    assert "hunter2" not in out
    assert out == name + "=<redacted>"
    long_prefix = "a" * 100 + "_password"
    assert at._redact_text(long_prefix + "=hunter2-not-real") == long_prefix + "=<redacted>"


def test_cap_boundary_never_leaks_part_of_a_credential():
    cap = at.MAX_REDACT_CHARS
    secrets = ("sk-proj-FAKEFAKEFAKEFAKEFAKEFAKE", "password=hunter2-not-real",
               "AWS_SECRET_ACCESS_KEY=hunter2-not-real")
    for secret in secrets:
        for offset in range(1, len(secret) + 1):
            for sep in (" ", "\n"):
                text = "x" * (cap - offset - len(sep)) + sep + secret
                out = at._redact_text(text)
                tail = out.split("... (truncated")[0].rstrip("x")
                for fragment in ("sk-", "hunter", "not-real", "FAKE"):
                    assert fragment not in tail, (secret, offset, sep, fragment)


def _best_of_three(func, payload):
    import time

    best = None
    for _ in range(3):
        started = time.perf_counter()
        func(payload)
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


def _assert_linear(func, make_payload):
    # Best of three at 10/20/50 KB. A linear scan grows ~5x from 10 to 50 KB, a
    # quadratic one ~25x; the 5 ms floor absorbs timer noise on tiny inputs.
    t10, t20, t50 = (_best_of_three(func, make_payload(kb * 1000)) for kb in (10, 20, 50))
    assert t50 <= max(0.005, 12 * t10), (t10, t20, t50)


def _repeat(unit, size):
    return (unit * (size // len(unit) + 1))[:size]



@pytest.mark.parametrize(
    "text, name",
    (
        ("tokenapi-key=S3CR3T-not-real", "tokenapi-key"),
        ("secret_access-key=S3CR3T-not-real", "secret_access-key"),
        ("aws_secret_access-key: S3CR3T-not-real", "aws_secret_access-key"),
        ("my_token_api-key = S3CR3T-not-real", "my_token_api-key"),
        ("tokentoken_api-key=S3CR3T-not-real", "tokentoken_api-key"),
        ("x-api-key: S3CR3T-not-real", "x-api-key"),
        ("credential-api-key: S3CR3T-not-real", "credential-api-key"),
    ),
)
def test_hyphenated_keyword_joins_are_redacted(text, name):
    # The old \b-anchored rule redacted these; a tempered prefix that stops at
    # the first keyword plus a suffix that stopped at '-' leaked them.
    assert at._redact_text(text) == name + "=<redacted>"


@pytest.mark.parametrize("unit", ("api-", "-key", "a-key", "access-", "token-key-", "x-api-key-"))
def test_hyphen_join_redaction_is_linear(unit):
    _assert_linear(at._redact_text, lambda size: _repeat(unit, size))


def _no_whitespace_filler(size):
    # Ends in a non-secret JSON pair so the URL that follows is not swallowed
    # by the preceding password value.
    unit = "password=" + "Q" * 990 + ","
    return _repeat(unit, size - 8) + ',"k":"x"'


def test_cap_cut_inside_url_userinfo_without_whitespace_never_leaks():
    # No whitespace anywhere: the old window fallback kept a password fragment
    # with no '@', and once the earlier values shrank to "<redacted>" that
    # fragment landed inside the visible 4000-char block.
    floor = at.MAX_REDACT_CHARS - 256
    text = (_no_whitespace_filler(floor - 53) + "https://user:" + "PASSWORDPART"
            + "P" * 600 + "@host/" + "Z" * 100)
    out = at._redact_text(text)
    assert "PASSWORDPART" not in out
    assert "PASSWORDPART" not in at._block(out, at.MAX_EVENT_BLOCK)
    assert "PASSWORDPART" not in at._preview_descriptor(text)["text"]


@pytest.mark.parametrize("offset", (5, 40, 300, 700))
def test_cap_cut_after_shrinking_values_never_leaks_a_url_fragment(offset):
    cap = at.MAX_REDACT_CHARS
    url = "https://user:" + "SECRETPART" + "S" * 1000 + "@host/"
    text = _no_whitespace_filler(cap - offset - 13) + url + "Z" * 100
    out = at._redact_text(text)
    assert len(out) < 4000
    assert "SECRETPART" not in out and "SSSS" not in out


@pytest.mark.parametrize(
    "value", ('"correct horse battery staple"', "'correct horse battery staple'"),
)
def test_cap_cut_inside_a_quoted_value_never_leaks_a_word(value):
    cap = at.MAX_REDACT_CHARS
    secret = "password = " + value
    for cut_after in ("correct", "correct ", "correct horse", "correct horse ",
                      "correct horse battery "):
        keep = secret.index(cut_after) + len(cut_after)
        text = "x " * ((cap - keep) // 2) + "x" * ((cap - keep) % 2) + secret + " tail"
        assert text[:cap].endswith(cut_after), cut_after
        out = at._redact_text(text)
        for word in ("correct", "horse", "battery", "staple"):
            assert word not in out, (cut_after, word)


def test_truncation_alone_is_not_reported_as_redaction():
    text = "plain output line\n" * (at.MAX_REDACT_CHARS // 10)
    preview = at._preview_descriptor(text)
    assert preview["truncated"] is True
    assert preview["redacted"] is False
    descriptor = at._safe_value_descriptor(text, at._block(at._redact_text(text), at.MAX_EVENT_BLOCK))
    assert descriptor["truncated"] is True
    assert descriptor["redacted"] is False


def test_truncated_input_with_a_secret_is_still_reported_as_redacted():
    text = "password=hunter2-not-real\n" + "plain output line\n" * (at.MAX_REDACT_CHARS // 10)
    preview = at._preview_descriptor(text)
    assert preview["truncated"] is True
    assert preview["redacted"] is True
    assert "hunter2" not in preview["text"]
