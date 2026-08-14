import os

import debug_dump
from sonder_logging import REDACTED, Redactor


def test_write_dump_creates_text_file(tmp_path):
    path = debug_dump.write_dump(
        tmp_path,
        label="bug report",
        messages=[{"role": "user", "content": "/quality"}],
        sections=[("context", "healthy")],
        events=[{"role": "assistant/model", "content": "answer"}],
    )

    text = open(path, encoding="utf-8").read()
    assert "sonder debug dump" in text
    assert "label: bug report" in text
    assert "/quality" in text
    assert "== context ==" in text
    assert "answer" in text


def test_write_dump_redacts_messages_sections_events_and_label(tmp_path):
    secret = "dump-secret-value-12345"
    path = debug_dump.write_dump(
        tmp_path,
        label="Authorization: Bearer %s" % secret,
        messages=[{"role": "user", "content": "api_key=%s" % secret}],
        sections=[("token=%s" % secret, "password: %s" % secret)],
        events=[{"role": "assistant", "content": "Bearer %s" % secret}],
        redactor=Redactor(secret_values=(secret,)),
    )

    text = open(path, encoding="utf-8").read()
    assert secret not in text
    assert secret not in path
    assert REDACTED in text


def test_dump_filename_is_opaque_and_distinct_for_identical_labels(tmp_path):
    # Labels can contain private project/path information even when they are
    # not credentials. They belong in the developer-selected dump body, never
    # its directory entry or a predictable overwrite target.
    label = r"C:\\Users\\natew\\private-project"
    first = debug_dump.write_dump(tmp_path, label=label)
    second = debug_dump.write_dump(tmp_path, label=label)

    assert first != second
    for path in (first, second):
        name = os.path.basename(path)
        assert name.startswith("sonder-dump-")
        assert "private" not in name
        assert "Users" not in name
        assert open(path, encoding="utf-8").read().startswith("sonder debug dump")
