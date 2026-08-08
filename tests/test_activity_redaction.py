"""_redact_text is the only sanitizer on the activity ledger's free-text fields.

record_tool_result stores tool stdout and summaries through it, and those reach
snapshot() -> the sonder_activity field on chat responses and the status payload.
It had drifted behind npu_contract's rule set, which does the same job on the
same shapes; these pin the shapes that were measured passing through it.
"""
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
