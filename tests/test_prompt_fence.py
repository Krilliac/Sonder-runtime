"""Two boundary defects in how a turn's prompts are assembled.

#36 -- an unterminated ``` fence inside injected material swallows everything
after it. Measured on the real ``server._answer`` path: a stored fact whose
body opened a ```python block and never closed it put the ``# Task:`` marker,
the task itself, and the "answer only the task below" directive INSIDE that
code block. The model then reads the question as part of the fact's sample
code. ``recall._format`` already guards the version of this it creates itself
(its 400-char cut can land mid-block, so it appends a closer); this is the same
family arriving from a different source, and it takes the same shape -- close
the block at the item boundary. The stored text is never truncated or
rewritten: a fact cut in half is a fact that lies, so the ONLY thing assembly
may add is a terminator after the whole fact.

#33 -- one turn builds the system prompt more than once, reading
system_profile.md from disk each time. Measured: a routed work request builds it
twice (execution-mode router, then the workbench agent) and a workbench-agent
turn builds it twice (agent, then the negative-claim reviewer). Both prompts
reach a model; neither is discarded. An edit landing between two reads puts two
DIFFERENT standing-instruction sets into one turn.
"""
import json

import memory_store as ms
import orchestrator as o
import server
import system_profile


# --- #36 ---------------------------------------------------------------------

FENCED_FACT = (
    "AUDIT PROBE fence fact: the Debug workaround is this snippet.\n"
    "```python\n"
    "def workaround():\n"
    "    return 'CMAKE_MSVC_DEBUG_INFORMATION_FORMAT=Embedded'\n"
)
TILDE_FACT = (
    "AUDIT PROBE tilde fact: the Debug workaround is this snippet.\n"
    "~~~python\n"
    "def workaround():\n"
    "    return 'CMAKE_MSVC_DEBUG_INFORMATION_FORMAT=Embedded'\n"
)
QUESTION = "what about other Debug related settings"


def _inside_open_fence(prompt, needle):
    """Whether `needle` sits inside a code fence nothing ever closed.

    Scans the prompt text ahead of `needle` the way a Markdown reader does:
    a bare run of >=3 ` or ~ opens a block, and only a bare run of at least the
    same length of the SAME character closes it.
    """
    head = prompt[:prompt.index(needle)]
    char = None
    length = 0
    for line in head.splitlines():
        stripped = line.lstrip()
        if char is None:
            for candidate in ("`", "~"):
                run = len(stripped) - len(stripped.lstrip(candidate))
                if run >= 3:
                    char, length = candidate, run
                    break
            continue
        run = len(stripped) - len(stripped.lstrip(char))
        if run >= length and not stripped[run:].strip():
            char, length = None, 0
    return char is not None


def _would_swallow(item):
    """Whether this fixture leaves a fence open at all.

    Without this, a fixture edited to a fact with no fence (or a balanced one)
    makes every assertion below pass having exercised nothing.
    """
    return _inside_open_fence("%s\n\n# Task:\nq" % item, "# Task:")


def test_an_unterminated_fence_in_a_fact_cannot_swallow_the_task():
    # Guard: the fixture must really be dangerous before assembly.
    assert _would_swallow(FENCED_FACT), "the fixture fact closes its own fence"
    p = o.build_prompt(QUESTION, [], facts=[FENCED_FACT])
    # Guard: the fact must actually reach the prompt, or nothing was exercised.
    assert FENCED_FACT.strip() in p
    assert not _inside_open_fence(p, "# Task:"), (
        "the '# Task:' marker fell inside the fact's unterminated code fence")
    assert not _inside_open_fence(p, QUESTION), (
        "the task text fell inside the fact's unterminated code fence")
    assert not _inside_open_fence(p, o.TASK_DIRECTIVE), (
        "the answer-only-the-task directive fell inside the fact's fence")


def test_an_unterminated_tilde_fence_in_a_fact_cannot_swallow_the_task():
    # ~~~ is a code fence too. A guard that only counts ``` leaves the same
    # defect reachable through the other fence character.
    assert _would_swallow(TILDE_FACT), "the fixture fact closes its own fence"
    p = o.build_prompt(QUESTION, [], facts=[TILDE_FACT])
    assert TILDE_FACT.strip() in p
    assert not _inside_open_fence(p, "# Task:")


def test_an_unterminated_fence_in_a_recall_cannot_swallow_the_task():
    recall = "how do I build it -> like this\n```python\nprint('hi')\n"
    assert _would_swallow(recall), "the fixture recall closes its own fence"
    p = o.build_prompt(QUESTION, [], recalls=[recall])
    assert recall.strip() in p
    assert not _inside_open_fence(p, "# Task:")


def test_an_unterminated_fence_in_a_lesson_cannot_swallow_the_task():
    lesson = "prefer /Z7 over /Zi\n```cmake\nset(CMAKE_MSVC_DEBUG ...)\n"
    assert _would_swallow(lesson), "the fixture lesson closes its own fence"
    p = o.build_prompt(QUESTION, [lesson])
    assert lesson.strip() in p
    assert not _inside_open_fence(p, "# Task:")


def test_a_fact_that_opened_a_fence_is_still_rendered_whole_and_unshortened():
    """Lock-in, not a RED test: passes at the parent and must keep passing.

    The fix may only ADD a terminator after the item. Editing, closing early,
    or dropping any of the operator's stored bytes would violate the invariant
    the char-budget work already pinned.
    """
    p = o.build_prompt(QUESTION, [], facts=[FENCED_FACT])
    assert FENCED_FACT.rstrip() in p
    assert o.FACTS_OMITTED_PREFIX not in p


def test_a_balanced_fence_is_left_exactly_as_stored():
    """Lock-in against over-closing: a well-formed fact gains nothing."""
    balanced = "fine fact\n```python\nprint('hi')\n```\ntrailing prose"
    p = o.build_prompt(QUESTION, [], facts=[balanced])
    section = p.split(o.FACT_ITEM_HEADER, 1)[1].split(o.TASK_DIRECTIVE, 1)[0]
    assert section.count("```") == 2, "a balanced fact must not gain a fence"
    assert not _inside_open_fence(p, "# Task:")


def _stub_model(monkeypatch):
    monkeypatch.setattr(server.embeddings, "embed", lambda _text: None)
    monkeypatch.setattr(server.embeddings, "valid_vector", lambda _value: False)
    monkeypatch.setattr(
        server, "_make_generate", lambda *_a, **_k: (lambda _prompt: "unused"))
    server.activity_tracker.reset_for_tests()


def test_live_composition_survives_a_stored_fact_with_an_unterminated_fence(
    monkeypatch,
):
    """The real server._answer composition -- no model involved.

    Which facts reach the prompt, and how they are rendered, is deterministic.
    """
    conn = ms.connect(":memory:")
    ms.add_fact(conn, "fence-fact", "sonder", FENCED_FACT)
    assert _would_swallow(FENCED_FACT), "the fixture fact closes its own fence"
    _stub_model(monkeypatch)

    _resp, _iid, trace = server._answer(
        conn, QUESTION, "model", "system", 0.2, 128, 2048,
        "session", "sonder", None, trace=True,
    )
    prompt = trace["augmented_prompt"]
    # Guard: without the stored fact in the prompt this proves nothing.
    assert FENCED_FACT.strip() in prompt
    assert not _inside_open_fence(prompt, "# Task:"), (
        "a stored fact's unterminated fence swallowed the live task marker")
    assert not _inside_open_fence(prompt, QUESTION)


# --- #33 ---------------------------------------------------------------------

def _profile_that_changes_on_every_read(monkeypatch, tmp_path):
    """Point the profile at tmp_path and rewrite it after every read.

    Models the supported live edit: system_profile.md is documented as editable
    while the server runs. Rewriting on every read makes the divergence
    deterministic instead of depending on when an operator happens to save.
    """
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        server.system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)
    system_profile.write_profile("PROBE standing instruction revision 0.")

    state = {"n": 0}
    real_read = system_profile.read_profile

    def rewriting_read(path=None):
        out = real_read(path)
        state["n"] += 1
        system_profile.write_profile(
            "PROBE standing instruction revision %d." % state["n"])
        return out

    monkeypatch.setattr(system_profile, "read_profile", rewriting_read)
    monkeypatch.setattr(server.system_profile, "read_profile", rewriting_read)
    return state


def _record_builds(monkeypatch):
    built = []
    real_build = server._build_system

    def recording_build(system, trace, persona):
        out = real_build(system, trace, persona)
        built.append(out)
        return out

    monkeypatch.setattr(server, "_build_system", recording_build)
    return built


def _standing_revisions(built):
    """The distinct 'PROBE standing instruction revision N.' lines seen."""
    seen = set()
    for text in built:
        for line in text.splitlines():
            if line.startswith("PROBE standing instruction revision"):
                seen.add(line.strip())
    return seen


def _stub_agent_model(monkeypatch, router_json=None):
    final = json.dumps({
        "action": "final",
        "final": "I searched the tree; the retry helper does not exist in this repository.",
    })
    review = json.dumps({"decision": "accept", "reason": "anchor searched"})

    def fake_make_generate(model, system, *_a, **_k):
        def gen(prompt, history=None):
            if router_json and "execution-mode router" in (system or ""):
                return router_json
            if "evidence reviewer" in (system or ""):
                return review
            return final
        gen.last_usage = {}
        gen.last_response_meta = {}
        return gen

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    monkeypatch.setattr(server, "resolve_sonder_model", lambda _strict: "stub-model")
    monkeypatch.setattr(server.embeddings, "embed", lambda _text: None)
    monkeypatch.setattr(server.embeddings, "valid_vector", lambda _value: False)
    server.activity_tracker.reset_for_tests()


def test_two_prompts_in_one_agent_turn_carry_the_same_standing_instructions(
    monkeypatch, tmp_path,
):
    """A workbench-agent turn builds the system prompt twice.

    Once for the agent loop, once for the negative-claim reviewer at
    finalization. Both go to a model. They must not disagree about the
    operator's standing instructions.
    """
    _profile_that_changes_on_every_read(monkeypatch, tmp_path)
    built = _record_builds(monkeypatch)
    _stub_agent_model(monkeypatch)

    server._agent_impl(
        "does this repo contain a retry helper", tier="code", max_steps=2,
        allow_web=False, project="", allow_location=False,
    )

    # Guard: one build trivially agrees with itself. If the reviewer stopped
    # firing, this test would pass having exercised nothing.
    assert len(built) == 2, (
        "expected the agent turn to build the system prompt twice, saw %d"
        % len(built))
    revisions = _standing_revisions(built)
    assert len(revisions) == 1, (
        "one turn sent two different standing-instruction sets to the model: %s"
        % sorted(revisions))


def test_two_prompts_in_one_routed_turn_carry_the_same_standing_instructions(
    monkeypatch, tmp_path,
):
    """A routed work request builds the system prompt for two consumers.

    The execution-mode router, then the lane it chose. Different base
    instructions, so the strings are not interchangeable -- but the operator's
    standing instructions are shared context and must be the same in both.
    """
    _profile_that_changes_on_every_read(monkeypatch, tmp_path)
    built = _record_builds(monkeypatch)
    _stub_agent_model(monkeypatch, router_json=json.dumps({
        "mode": "workbench", "tier": "code",
        "reason": "self-contained bounded task", "confidence": 0.9,
    }))
    monkeypatch.setattr(server.npu_service, "routing_active", lambda: "off")
    monkeypatch.setattr(server.npu_service, "route_decide", lambda _p: None)

    out = server.route_work_request(
        "please update the parser, add tests, and make sure everything still builds",
        project="",
    )
    # Guard: an ineligible prompt returns None and builds nothing.
    assert out is not None, "the probe prompt was not routed; nothing was built"
    assert "bounded local mode model" in out, (
        "the execution-mode router model did not run, so only one prompt was "
        "built and the test proves nothing")
    assert len(built) >= 2, (
        "expected at least two builds in a routed turn, saw %d" % len(built))
    revisions = _standing_revisions(built)
    assert len(revisions) == 1, (
        "one turn sent %d different standing-instruction sets to the model: %s"
        % (len(revisions), sorted(revisions)))


def test_a_later_turn_still_sees_an_edited_profile(monkeypatch, tmp_path):
    """Lock-in: pinning is per-turn, never a process-lifetime cache.

    system_profile.py exists so an operator can edit standing instructions
    while the server runs. A fix that froze the profile for the life of the
    process would trade one defect for a worse one.
    """
    monkeypatch.setattr(system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        server.system_profile, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_SYSTEM_PROFILE", raising=False)

    system_profile.write_profile("PROBE turn-one instruction.")
    first = server._build_system("base", False, "")
    system_profile.write_profile("PROBE turn-two instruction.")
    second = server._build_system("base", False, "")

    assert "PROBE turn-one instruction." in first
    assert "PROBE turn-two instruction." in second, (
        "an edit between turns must reach the next turn's prompt")
