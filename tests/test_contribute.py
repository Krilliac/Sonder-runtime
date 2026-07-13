import memory_store as ms
import contribute
import time


def _conn():
    return ms.connect(":memory:")


def test_is_shareable_generic_sentence():
    assert contribute.is_shareable("Use two pointers to merge sorted arrays.") is True


def test_is_shareable_rejects_windows_path():
    assert contribute.is_shareable("cd C:\\\\Users\\\\bob\\\\secret") is False


def test_is_shareable_rejects_api_key():
    assert contribute.is_shareable("remember api_key=sk-abcdef1234567890 for the client") is False


def test_is_shareable_rejects_email():
    assert contribute.is_shareable("contact bob@example.com for help with this bug") is False


def test_is_shareable_rejects_long_text():
    assert contribute.is_shareable("x" * 400) is False


def test_privacy_review_helpers_name_and_redact_findings():
    text = "contact bob@example.com and use api_key=sk-super-private"

    reasons = contribute.private_reasons(text)
    preview = contribute.privacy_preview(text)

    assert reasons == ["email", "credential_assignment", "known_credential"]
    assert "bob@example.com" not in preview
    assert "sk-super-private" not in preview
    assert "<email>" in preview
    assert "<credential>" in preview


def test_privacy_rules_cover_common_header_json_cloud_and_system_path_forms():
    samples = {
        "authorization_header": "Authorization: Bearer sk-proj-abcdefghijklmnop",
        "credential_assignment": '{"api_key": "sk-proj-abcdefghijklmnop"}',
        "sensitive_header": "Cookie: sessionid=abcdefghijklmnop",
        "known_credential": "aws id AKIAABCDEFGHIJKLMNOP",
        "unix_system_path": "read /etc/ssh/id_rsa before connecting",
        "windows_path": "read D:/secrets/token.txt before connecting",
        "tilde_private_path": "read ~/.aws/credentials before connecting",
        "url_credentials": "fetch https://user:private-pass@example.invalid/repo",
        "environment_home_path": r"read %USERPROFILE%\.ssh\id_rsa",
        "file_uri": "read file:///home/alice/private.txt",
        "workspace_path": "read /workspace/acme-private/config.json",
        "relative_private_path": "read secrets/production-token.txt",
    }

    for expected, text in samples.items():
        reasons = contribute.private_reasons(text)
        preview = contribute.privacy_preview(text)
        assert expected in reasons
        assert contribute.is_shareable(text) is False
        assert "private-pass" not in preview
        assert "abcdefghijklmnop" not in preview
        assert "id_rsa" not in preview
        assert "credentials" not in preview
        assert "sessionid" not in preview


def test_privacy_rules_cover_vendor_prefixed_credentials_and_api_headers():
    samples = (
        "X-API-Key: abcdefghijklmnop",
        "x-auth-token: abcdefghijklmnop",
        "Set-Cookie: sessionid=abcdefghijklmnop; HttpOnly",
        "OPENAI_API_KEY=abcdefghijklmnop",
        "ANTHROPIC_API_KEY=abcdefghijklmnop",
        "Authorization: Token glpat-abcdefghijklmnopqrstuvwxyz123456",
        "token hf_abcdefghijklmnopqrstuvwxyz123456",
        "token npm_abcdefghijklmnopqrstuvwxyz123456",
        "sessionid=abcdefghijklmnopqrstuvwxyz123456",
        "opaque " + "aB3_" * 16,
    )

    for text in samples:
        assert contribute.private_reasons(text)
        assert contribute.is_shareable(text) is False
        assert "abcdefghijklmnop" not in contribute.privacy_preview(text)


def test_privacy_scan_is_bounded_on_long_non_secret_identifier_text():
    for text in (
        "foo_" * 4000 + "bar=1",
        "x-" * 8000 + "noop",
    ):
        started = time.perf_counter()
        reasons = contribute.private_reasons(text)
        elapsed = time.perf_counter() - started

        assert reasons == []
        assert elapsed < 0.5


def test_private_key_header_scan_is_linear_and_rejects_incomplete_material():
    text = ("-----BEGIN PRIVATE KEY-----\n" * 1000) + "no footer"
    started = time.perf_counter()

    reasons = contribute.private_reasons(text)

    assert "private_key" in reasons
    assert time.perf_counter() - started < 0.5


def test_privacy_preview_never_leaks_path_suffixes_with_spaces():
    preview = contribute.privacy_preview(
        r"open C:\Users\alice\My Documents\private notes.txt"
    )

    assert preview == "<windows-path>"
    assert "Documents" not in preview
    assert "private" not in preview


def test_scrubbed_lessons_filters_mixed_db():
    c = _conn()
    ms.add_lesson(c, "1", "Use a set for O(1) membership tests.", None, "int1")
    ms.add_lesson(c, "2", "see C:\\\\Users\\\\bob\\\\notes for details", None, "int2")
    ms.add_lesson(c, "3", "Prefer early returns over deep nesting.", None, "int3")
    ms.add_lesson(c, "4", "y" * 400, None, "int4")

    result = contribute.scrubbed_lessons(c)

    texts = {lesson["text"] for lesson in result}
    assert texts == {
        "Use a set for O(1) membership tests.",
        "Prefer early returns over deep nesting.",
    }
    for lesson in result:
        assert set(lesson.keys()) == {"id", "text"}
        assert lesson["id"].startswith("lesson-")
        assert len(lesson["id"]) == len("lesson-") + 24


def test_scrubbed_lessons_never_exports_arbitrary_local_identifier():
    c = _conn()
    private_id = "alice@example.com"
    ms.add_lesson(c, private_id, "Prefer immutable data at API boundaries.", None, "int")

    result = contribute.scrubbed_lessons(c)

    assert len(result) == 1
    assert private_id not in repr(result)
