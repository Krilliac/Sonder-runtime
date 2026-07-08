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
