"""``session_export`` text output applies the durable-export redaction policy."""
import pytest

import memory_store
import server

from sonder_runtime.application.session import transcript_export


def _legacy_format(session_id, sess, turns):
    """The pre-redaction ``session_export`` rendering, kept verbatim for parity."""
    lines = [
        "session: %s" % session_id,
        "title: %s" % (sess.get("title") or "(untitled)"),
        "project: %s" % (sess.get("project") or "(none)"),
        "",
    ]
    for turn in turns:
        lines.append("USER: %s" % (turn.get("task") or ""))
        lines.append("ASSISTANT: %s" % (turn.get("response") or ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def _seed(monkeypatch, tmp_path, turns, *, title="demo", project="proj"):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.touch_session(conn, "S1", project)
        if title:
            memory_store.set_session_title(conn, "S1", title)
        for index, (task, response) in enumerate(turns):
            memory_store.log_interaction(
                conn, "I%d" % index, task, "", response, "code", session_id="S1",
            )
    finally:
        conn.close()


_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
    "-----END RSA PRIVATE KEY-----"
)

# (stored text, raw secret that must not survive the export)
SECRET_CASES = [
    ("hello, password=hunter2", "hunter2"),
    ("my key is sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
    ("use sk-proj-AbCdEfGhIjKlMnOpQrStUvWx", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
    ("curl -H 'Bearer eyJhbGciOiJIUzI1NiJ9abcdefgh'", "eyJhbGciOiJIUzI1NiJ9abcdefgh"),
    ("Authorization: Bearer abc.def.ghi-jkl", "abc.def.ghi-jkl"),
    ("aws AKIAIOSFODNN7EXAMPLE in env", "AKIAIOSFODNN7EXAMPLE"),
    ("secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    ("api_key=abcd1234efgh5678", "abcd1234efgh5678"),
    ("here is my key\n" + _PEM, "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun"),
    ("clone https://alice:hunter2pw@example.com/repo.git", "hunter2pw"),
]


@pytest.mark.parametrize("stored, secret", SECRET_CASES)
def test_session_export_redacts_task_secrets(monkeypatch, tmp_path, stored, secret):
    _seed(monkeypatch, tmp_path, [(stored, "ok")])
    out = server.session_export("S1")
    assert secret not in out
    assert "[REDACTED]" in out
    assert "ASSISTANT: ok" in out


@pytest.mark.parametrize("stored, secret", SECRET_CASES)
def test_session_export_redacts_response_secrets(monkeypatch, tmp_path, stored, secret):
    _seed(monkeypatch, tmp_path, [("what is it?", stored)])
    out = server.session_export("S1")
    assert secret not in out
    assert "[REDACTED]" in out
    assert "USER: what is it?" in out


# Shapes the shared redaction policy (domain.security.redaction.PATTERNS, kept
# in lockstep with platform.logging) does not recognise yet. session_export
# deliberately reuses that one policy rather than growing a private variant, so
# these stay visible here: strict xfail flips to a failure the moment the
# shared policy learns them, prompting removal of the marker.
KNOWN_POLICY_GAPS = [
    ("db creds pwd: s3cretPass!", "s3cretPass!"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
     "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
]


@pytest.mark.xfail(strict=True, reason="not in the shared redaction policy yet")
@pytest.mark.parametrize("stored, secret", KNOWN_POLICY_GAPS)
def test_session_export_known_policy_gaps(monkeypatch, tmp_path, stored, secret):
    _seed(monkeypatch, tmp_path, [(stored, "ok")])
    assert secret not in server.session_export("S1")


def test_session_export_password_example_before_after(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, [("hello, password=hunter2", "noted")])
    out = server.session_export("S1")
    assert out == (
        "session: S1\n"
        "title: demo\n"
        "project: proj\n"
        "\n"
        "USER: hello, password=[REDACTED]\n"
        "ASSISTANT: noted"
    )


def test_session_export_redacts_title(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, [("hi", "hello")], title="login password=hunter2")
    out = server.session_export("S1")
    assert "hunter2" not in out
    assert "title: login password=[REDACTED]" in out


def test_multiline_secret_cannot_swallow_turn_structure(monkeypatch, tmp_path):
    # An Authorization value may span one whitespace gap; redacting per field
    # keeps it from eating the following ASSISTANT: marker.
    _seed(monkeypatch, tmp_path, [("Authorization: Basic", "fine answer")])
    out = server.session_export("S1")
    assert "ASSISTANT: fine answer" in out


SECRET_FREE_TURNS = [
    ("hello", "hi back"),
    ("how do I reset my password?", "Open Settings > Account > Reset password."),
    ("explain token buckets", "A token bucket refills at rate r; each request spends a token."),
    ("", "empty task row"),
    ("multi\nline  task\twith tabs", "  leading and trailing spaces  "),
    ("unicode: café — 日本", "emoji \U0001F600 ok"),
    ("the secret to good bread is time", "sk-short is not a key"),
]


def test_secret_free_transcript_is_byte_identical(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, SECRET_FREE_TURNS, title="parity", project="proj")
    conn = server._open_db()
    try:
        sess = memory_store.get_session(conn, "S1")
        turns = memory_store.session_turns(conn, "S1")[-50:]
    finally:
        conn.close()
    expected = _legacy_format("S1", sess, turns)
    out = server.session_export("S1")
    assert out.encode("utf-8") == expected.encode("utf-8")
    assert "[REDACTED]" not in out


def test_secret_free_untitled_defaults_unchanged(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, [("hello", "")], title="", project="")
    conn = server._open_db()
    try:
        sess = memory_store.get_session(conn, "S1")
        turns = memory_store.session_turns(conn, "S1")
    finally:
        conn.close()
    assert server.session_export("S1") == _legacy_format("S1", sess, turns)


def test_redactor_failure_is_fail_closed(monkeypatch):
    def boom(_text, **_kwargs):
        raise RuntimeError("redactor broke")

    monkeypatch.setattr(transcript_export._redaction, "redact_text", boom)
    rendered = transcript_export.format_session_transcript(
        "S1", {"title": "t", "project": "p"}, [{"task": "password=hunter2", "response": "x"}],
    )
    assert "hunter2" not in rendered
    assert "USER: [REDACTION_FAILED]" in rendered


def test_configured_secret_value_is_redacted_without_a_label(monkeypatch, tmp_path):
    # A bare configured secret has no shape a pattern can see; the export must
    # use the same value-aware runtime redactor as durable capture.
    monkeypatch.setenv("SONDER_API_KEY", "plainvalue-7f3a9c")
    _seed(monkeypatch, tmp_path, [("my key is plainvalue-7f3a9c ok", "noted")])
    out = server.session_export("S1")
    assert "plainvalue-7f3a9c" not in out
    assert "USER: my key is [REDACTED] ok" in out


def test_graph_config_secret_value_is_redacted(monkeypatch, tmp_path):
    import dataclasses
    from types import SimpleNamespace

    @dataclasses.dataclass
    class _Secrets:
        api_key: str = "graph-held-secret-91b2"

    monkeypatch.setattr(
        server, "_APP_GRAPH",
        SimpleNamespace(config=SimpleNamespace(secrets=_Secrets(), private_source_paths=())),
    )
    _seed(monkeypatch, tmp_path, [("hi", "value graph-held-secret-91b2 here")])
    out = server.session_export("S1")
    assert "graph-held-secret-91b2" not in out
    assert "ASSISTANT: value [REDACTED] here" in out


def test_injected_redactor_is_applied_then_export_policy(monkeypatch):
    rendered = transcript_export.format_session_transcript(
        "S1", {"title": "t", "project": "p"},
        [{"task": "zeta password=hunter2", "response": "ok"}],
        redact=lambda text: text.replace("zeta", "[REDACTED]"),
    )
    assert "USER: [REDACTED] password=[REDACTED]" in rendered
