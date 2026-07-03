import memory_store as ms
import contribute


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


def test_scrubbed_lessons_filters_mixed_db():
    c = _conn()
    ms.add_lesson(c, "1", "Use a set for O(1) membership tests.", None, "int1")
    ms.add_lesson(c, "2", "see C:\\\\Users\\\\bob\\\\notes for details", None, "int2")
    ms.add_lesson(c, "3", "Prefer early returns over deep nesting.", None, "int3")
    ms.add_lesson(c, "4", "y" * 400, None, "int4")

    result = contribute.scrubbed_lessons(c)

    ids = {l["id"] for l in result}
    assert ids == {"1", "3"}
    for l in result:
        assert set(l.keys()) == {"id", "text"}
