"""A stored fact must never be readable as the task.

Live failure this pins: the operator asked "what about other Debug related
settings" and got a 167-second essay on `parallel_run_code` timeout accounting
-- the verbatim subject of a stored project fact roughly ten times longer than
the question, sitting above it under a header asserting it is always true.
The question was never addressed. The probe fact only revealed the shape; any
long stored fact can displace any short question.

Most of these are ASSEMBLY-layer assertions: they prove the task is
structurally distinguishable from injected context in the prompt text. They do
not and cannot prove what a model does with it.

`test_live_composition_*` is different and is NOT model-dependent: which facts
reach the prompt is pure deterministic composition, so it drives the real
`server._answer` path against an in-memory DB with a stub generate_fn. An
earlier version of this file tested `select_facts` on a bare two-element list
the live path never produces, and so could not see that twelve preferences
evict every project fact.
"""
import memory_store as ms
import orchestrator as o
import preference_learning as preferences
import server


# The live fact, verbatim in shape: multi-line, carries a repro, a symptom, a
# verdict, and its own bullets. Task-shaped, not fact-shaped.
TASK_SHAPED_FACT = (
    "AUDIT2 PROBE: parallel_run_code timeout accounting is self-contradictory.\n"
    "Repro (8 python jobs, max_workers=4, timeout=8, trivial code):\n"
    "- first-wave jobs a/b/c/d all report ~10.2-10.8s elapsed\n"
    "- second-wave jobs e/f/g/h report ~0.1s\n"
    "Run-to-run variance is under 0.4s. Confirmed real code defect, not model "
    "quality."
)
SHORT_QUESTION = "what about other Debug related settings"


def test_facts_block_declares_itself_reference_material_not_a_request():
    p = o.build_prompt(SHORT_QUESTION, [], facts=[TASK_SHAPED_FACT])
    header_and_preamble = p.split(o.FACT_ITEM_HEADER, 1)[0].lower()
    # The old header ("always true here") asserted authority and said nothing
    # about status: a block that is authoritative and ten times longer than the
    # question reads as the job. The block must say what it is NOT.
    assert "not instruction" in header_and_preamble or "never instruction" in header_and_preamble
    assert "reference" in header_and_preamble
    # and must say explicitly that answering a fact is not the job
    assert "do not answer" in header_and_preamble


def test_each_fact_is_fenced_so_a_multiline_fact_cannot_read_as_prompt_prose():
    # A fact body can be multi-line and carry its own "- " bullets, exactly as
    # recalls can. Joined as bare bullets there is no boundary: lines 2..N of
    # fact #1 read as ordinary prompt prose and a bulleted line inside a fact
    # reads as the start of fact #2.
    second = "AUDIT PROBE: the zqxwv marker phrase is jabberwock-42781."
    p = o.build_prompt(SHORT_QUESTION, [], facts=[TASK_SHAPED_FACT, second])
    section = p.split(o.FACTS_HEADER, 1)[1].split(o.TASK_DIRECTIVE, 1)[0]

    fences = [ln for ln in section.splitlines() if ln.startswith(o.FACT_ITEM_HEADER)]
    assert len(fences) == 2
    assert "%s 1 of 2" % o.FACT_ITEM_HEADER in section
    assert "%s 2 of 2" % o.FACT_ITEM_HEADER in section
    # the fence must not be confusable with a bullet a fact body could contain
    assert not o.FACT_ITEM_HEADER.startswith("- ")
    # and the fact's own bullets must not read as fact boundaries
    assert section.count("- second-wave jobs e/f/g/h report ~0.1s") == 1


def test_task_is_last_and_carries_an_answer_only_this_directive():
    p = o.build_prompt(
        SHORT_QUESTION, ["a lesson"], recalls=["a recall"], facts=[TASK_SHAPED_FACT],
    )
    assert o.TASK_DIRECTIVE in p
    # Nothing injected may sit between the directive and the task: the last
    # thing the model reads before the question must be the instruction to
    # answer the question.
    tail = p.split(o.TASK_DIRECTIVE, 1)[1]
    assert tail == "\n\n# Task:\n%s" % SHORT_QUESTION
    for injected in ("a lesson", "a recall", "parallel_run_code"):
        assert injected not in tail


def test_no_directive_when_nothing_was_injected():
    # A bare task with no retrieved context is still a passthrough, and the
    # /run compatibility block is a real instruction -- the directive must not
    # declare it "reference material".
    assert o.build_prompt("do X", [], recalls=[], facts=[]) == "do X"
    run_compat = o.build_prompt("make a pygame demo that will run with /run", [])
    assert o.TASK_DIRECTIVE not in run_compat


def test_bounded_facts_block_discloses_every_drop_and_shortens_nothing():
    facts = ["fact number %d: %s" % (n, "y" * 600) for n in range(20)]
    p = o.build_prompt("short question", [], facts=facts)
    kept, omitted = o.select_facts(facts)

    assert omitted > 0, "20 x ~600 chars must exceed the bound, or this proves nothing"
    assert len(kept) + omitted == len(facts)
    # every kept fact appears WHOLE -- a bound that shortens a fact silently is
    # the same defect class as a fact that displaces the task
    for text in kept:
        assert text in p
    for text in facts[len(kept):]:
        assert text not in p
    # and the prompt says so, in the prompt itself, with both counts
    disclosure = [
        ln for ln in p.splitlines() if ln.startswith(o.FACTS_OMITTED_PREFIX)
    ]
    assert len(disclosure) == 1
    assert str(omitted) in disclosure[0] and str(len(facts)) in disclosure[0]


def test_a_single_oversized_fact_is_never_cut_in_half():
    huge = "AUDIT PROBE: " + "z" * (o.MAX_FACTS_CHARS * 2)
    p = o.build_prompt("q", [], facts=[huge])
    assert huge in p
    assert o.FACTS_OMITTED_PREFIX not in p


CANARIES = (
    "AUDIT PROBE: the zqxwv marker phrase for memory recall audit is "
    "jabberwock-42781.",
    TASK_SHAPED_FACT,
)


def test_the_two_recall_canaries_survive_the_bound_verbatim():
    p = o.build_prompt(SHORT_QUESTION, [], project_facts=list(CANARIES))
    for text in CANARIES:
        assert text in p
    assert o.select_facts(project_facts=list(CANARIES))[1] == 0
    assert o.FACTS_OMITTED_PREFIX not in p


def test_a_flood_of_preferences_cannot_evict_a_project_fact():
    # The live path (server._answer) puts up to MAX_INJECTED_FACTS preferences
    # in front of the project facts, so a single shared budget consumed in list
    # order evicts every operator-authored fact by construction.
    prefs = ["User prefers thing %d." % n for n in range(40)]
    kept, omitted = o.select_facts(facts=prefs, project_facts=list(CANARIES))
    for text in CANARIES:
        assert text in kept
    assert omitted == (len(prefs) + len(CANARIES)) - len(kept)


def test_neither_fact_source_can_starve_the_other():
    prefs = ["User prefers thing %d." % n for n in range(40)]
    projects = ["project fact %d." % n for n in range(40)]
    kept, omitted = o.select_facts(facts=prefs, project_facts=projects)
    assert [t for t in kept if t in prefs], "preferences were starved out"
    assert [t for t in kept if t in projects], "project facts were starved out"
    # a fair split, not a first-come-first-served race won by call order
    assert abs(len([t for t in kept if t in prefs])
               - len([t for t in kept if t in projects])) <= 1
    assert omitted == 80 - len(kept)


def test_live_composition_keeps_both_canaries_behind_a_full_preference_set(monkeypatch):
    """The real server._answer composition, no model involved.

    Which facts reach the prompt is deterministic, so this is provable without
    a model: stub only the generator and the embedder, and let the real
    _preference_facts, facts_for_project, orchestrator and build_prompt run.
    """
    conn = ms.connect(":memory:")
    # Style preferences, because those are what `preference_applies` actually
    # admits for an arbitrary question -- topical ones are filtered out and
    # would make this test vacuous.
    index = 0
    for adjective in ("concise", "detailed", "short", "verbose", "formal",
                      "direct", "brief", "thorough", "plain", "structured"):
        for noun in ("answers", "explanations", "replies"):
            text = "User prefers %s %s." % (adjective, noun)
            ms.upsert_preference(
                conn, "pref-%d" % index, "global",
                preferences.preference_key(text), text, confidence=0.9,
            )
            index += 1
    for position, canary in enumerate(CANARIES):
        ms.add_fact(conn, "canary-%d" % position, "sonder", canary)

    # Guard against the vacuous version of this test: if the preference cap is
    # not saturated, nothing is competing with the canaries and passing proves
    # nothing.
    assert len(server._preference_facts(conn, SHORT_QUESTION)) == 12

    monkeypatch.setattr(server.embeddings, "embed", lambda _text: None)
    monkeypatch.setattr(server.embeddings, "valid_vector", lambda _value: False)
    monkeypatch.setattr(
        server, "_make_generate", lambda *_a, **_k: (lambda _prompt: "unused"))
    server.activity_tracker.reset_for_tests()

    _resp, _iid, trace = server._answer(
        conn, SHORT_QUESTION, "model", "system", 0.2, 128, 2048,
        "session", "sonder", None, trace=True,
    )

    prompt = trace["augmented_prompt"]
    for canary in CANARIES:
        assert canary in prompt, "operator canary evicted on the live path"
    assert prompt.rstrip().endswith(SHORT_QUESTION)


def test_turn_capture_and_trace_render_report_dropped_facts():
    # facts_omitted must have a consumer, or "reported" is a claim with no
    # surface behind it.
    server._TURN_TRACES.clear()
    ctx = {"lessons": [], "augmented_prompt": "p", "facts_omitted": 3}
    server._capture_turn("m", "code", ctx, "prompt", "response", iid="i")
    assert server._TURN_TRACES[-1]["facts_omitted"] == 3

    rendered = server._format_trace("m", "code", {}, ctx)
    assert "3" in rendered and "omitted" in rendered.lower()
    clean = server._format_trace("m", "code", {}, {"lessons": [], "augmented_prompt": "p"})
    assert "omitted" not in clean.lower()


def test_trace_reports_how_many_facts_were_left_out():
    conn = ms.connect(":memory:")
    facts = ["fact number %d: %s" % (n, "y" * 600) for n in range(20)]
    _resp, _iid, trace = o.run_with_learning_traced(
        conn, "short question", "code", lambda prompt: "answer",
        retrieve_fn=lambda c, t: [], id_fn=lambda: "iid", facts=facts,
    )
    assert trace["facts_omitted"] == o.select_facts(facts)[1]
    assert trace["facts_omitted"] > 0

    _resp, _iid, clean = o.run_with_learning_traced(
        conn, "short question", "code", lambda prompt: "answer",
        retrieve_fn=lambda c, t: [], id_fn=lambda: "iid2", facts=["one small fact"],
    )
    assert clean["facts_omitted"] == 0
