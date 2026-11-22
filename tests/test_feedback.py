import feedback


def test_classify_signal_richer_positive_actions():
    assert feedback.classify_signal("copied") == "copied"
    assert feedback.classify_signal("used it") == "used"
    assert feedback.classify_signal("edited it") == "edited"
    assert feedback.classify_signal("that worked") == "accepted"


def test_classify_signal_keeps_request_guard():
    assert feedback.classify_signal("write a copied example") is None
    assert feedback.classify_signal("why did it fail") is None
    assert feedback.classify_signal("nope") == "rejected"


def test_classify_signal_mixed_cues_are_ambiguous_not_rejected():
    # A bare "no" next to a positive cue must not record a rejection;
    # classify_feedback already treats these as ambiguous.
    for text in (
        "thanks, no problem",
        "perfect, no more errors",
        "great, no issues",
    ):
        assert feedback.classify_signal(text) is None, text
        assert feedback.classify_feedback(text) is None, text
    assert feedback.classify_signal("no, that's wrong") == "rejected"
    assert feedback.classify_signal("copied") == "copied"
