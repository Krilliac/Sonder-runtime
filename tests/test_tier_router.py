"""The transformation-vs-recall routing rule, from this project's model
measurements: local models are strong when the facts are in the prompt and weak
when they must be remembered."""
import tier_router as tr


def test_transformation_requests_classify_as_transformation():
    for prompt in (
        "Refactor this function to use a switch",
        "restructure this enum into a lookup",
        "```python\ndef f(): pass\n``` simplify this",
        "convert this struct to the 32-bit variant",
        "here is the function, add type hints",
    ):
        assert tr.classify(prompt) == "transformation", prompt


def test_recall_requests_classify_as_recall():
    for prompt in (
        "what is the exact Win32 ROP2 truth table",
        "which parameters does CreateFileW take",
        "list all the HID usage-page codes",
        "what's the default value of num_ctx",
    ):
        assert tr.classify(prompt) == "recall", prompt


def test_reasoning_requests_classify_as_reasoning():
    for prompt in (
        "why does this deadlock under two threads",
        "design a scheduler for these constraints",
        "prove this loop terminates",
    ):
        assert tr.classify(prompt) == "reasoning", prompt


def test_pasted_material_beats_a_recall_verb():
    # "which" is a recall cue, but the code is pasted -> transformation.
    prompt = "```python\ndef pick(): ...\n``` which branch is dead, remove it"
    assert tr.classify(prompt) == "transformation"


def test_route_prefers_local_for_transformation():
    r = tr.route("refactor this loop", available_tiers={"code", "reasoning"})
    assert r["kind"] == "transformation"
    assert r["tier"] == "code"
    assert not r["fallback_used"]


def test_route_prefers_cloud_for_recall():
    r = tr.route("what is the exact signature of WSARecv",
                 available_tiers={"code", "cloud-general", "reasoning"})
    assert r["kind"] == "recall"
    assert r["tier"] == "cloud-general"


def test_route_falls_back_when_preferred_tier_is_absent():
    # recall wants cloud, but only local tiers exist -> falls back, says so.
    r = tr.route("what is the RFC 5322 grammar", available_tiers={"code", "reasoning"})
    assert r["fallback_used"]
    assert r["tier"] in {"code", "reasoning"}
    assert "unavailable" in r["reason"]


def test_route_without_availability_names_the_preferred_tier():
    r = tr.route("recall the exact enum values")
    assert r["tier"] == "cloud-general"
    assert not r["fallback_used"]
