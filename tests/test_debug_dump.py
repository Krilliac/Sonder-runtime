import debug_dump


def test_write_dump_creates_text_file(tmp_path):
    path = debug_dump.write_dump(
        tmp_path,
        label="bug report",
        messages=[{"role": "user", "content": "/quality"}],
        sections=[("context", "healthy")],
        events=[{"role": "assistant/model", "content": "answer"}],
    )

    text = open(path, encoding="utf-8").read()
    assert "trilobite debug dump" in text
    assert "label: bug report" in text
    assert "/quality" in text
    assert "== context ==" in text
    assert "answer" in text
