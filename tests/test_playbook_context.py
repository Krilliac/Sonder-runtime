"""Prompt-side playbook routing tests; no server or model startup required."""

from dataclasses import dataclass

from sonder_runtime.application.memory.playbook_context import PlaybookContext


@dataclass
class Policy:
    max_index_bytes: int = 512
    max_context_bytes: int = 700
    max_topics: int = 3
    max_topics_per_turn: int = 2
    max_entry_bytes: int = 256


class Store:
    def __init__(self):
        self.policy = Policy()
        self.index = (
            "# Playbooks\n\n"
            "- [Builds](builds.md) — procedure — open when: build, compiler\n"
            "- [Shells](shells.md) — pitfall — open when: shell, powershell\n"
        )
        self.read_calls = []
        self.match_calls = 0
        self.reloads = 0

    def approved_index(self, session_id=""):
        self.reloads += 1
        return self.index

    def match(self, query):
        self.match_calls += 1
        raise AssertionError("request path must route from the frozen index")

    def read(self, topic, approved_only=True):
        self.read_calls.append((topic, approved_only))
        if topic == "builds":
            return [
                {"id": "approved-build", "title": "Pinned compiler", "body": "Use the wrapper.",
                 "evidence": "build.ps1", "status": "approved"},
                {"id": "pending", "title": "Pending note", "body": "Do not load me.",
                 "evidence": "", "status": "proposed"},
            ]
        return [{"id": "shell-note", "title": "PowerShell", "body": "Quote paths.",
                 "evidence": "shell check", "status": "approved"}]


def test_index_is_cached_per_session_and_reload_is_explicit():
    store = Store()
    context = PlaybookContext(lambda: store)
    first = context.stable_index("s1")
    assert first == "\n".join(store.index.splitlines()[2:]) + "\n"
    assert context.stable_index("s1") == first
    assert store.reloads == 1
    store.index = "- [Changed](changed.md) — procedure — open when: changed\n"
    assert context.stable_index("s1") == first
    assert context.stable_index("s2") == store.index
    assert store.reloads == 2
    assert context.reload_index("s1") == store.index
    assert store.reloads == 3


def test_selection_uses_index_triggers_and_approved_entries_only():
    store = Store()
    context = PlaybookContext(lambda: store)
    selection = context.select("please fix the compiler build", "s1")
    assert store.match_calls == 0
    assert store.read_calls == [("builds", True)]
    assert "Pinned compiler" in selection.text
    assert "Pending note" not in selection.text
    assert selection.topics == ({"topic": "builds", "entry_ids": ["approved-build"]},)


def test_relevance_prefers_topic_with_more_trigger_overlap_and_honors_caps():
    store = Store()
    store.index = (
        "- [Shells](shells.md) — pitfall — open when: shell\n"
        "- [Builds](builds.md) — procedure — open when: build, compiler\n"
    )
    context = PlaybookContext(lambda: store)
    selection = context.select("compiler build", "s1")
    assert selection.topics[0]["topic"] == "builds"
    assert len(selection.text.encode("utf-8")) <= store.policy.max_context_bytes


def test_empty_playbooks_preserve_empty_prompt_fragment():
    class Empty:
        policy = Policy()
        def approved_index(self, session_id=""): return ""
        def read(self, topic, approved_only=True): return []

    context = PlaybookContext(lambda: Empty())
    assert context.stable_index("s1") == ""
    assert context.select("anything", "s1").text == ""


def test_unicode_byte_caps_apply_to_index_and_rendered_frame():
    store = Store()
    store.policy.max_index_bytes = 64
    store.policy.max_context_bytes = 160
    store.index = "- [測定](測定.md) — measurement — open when: 測定, gpu\n"
    context = PlaybookContext(lambda: store)
    assert len(context.stable_index("s1").encode("utf-8")) <= 64
    selection = context.select("gpu", "s1")
    assert len(selection.text.encode("utf-8")) <= 160
