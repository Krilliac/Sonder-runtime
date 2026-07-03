import feedback


def test_positive_thanks_that_worked():
    assert feedback.classify_feedback("thanks, that worked") == "positive"


def test_positive_perfect():
    assert feedback.classify_feedback("perfect") == "positive"


def test_positive_nice_that_did_it():
    assert feedback.classify_feedback("nice that did it") == "positive"


def test_positive_exactly_thank_you():
    assert feedback.classify_feedback("exactly, thank you") == "positive"


def test_negative_no_thats_wrong():
    assert feedback.classify_feedback("no that's wrong") == "negative"


def test_negative_still_errors():
    assert feedback.classify_feedback("still errors") == "negative"


def test_negative_doesnt_work():
    assert feedback.classify_feedback("doesn't work") == "negative"


def test_negative_nope():
    assert feedback.classify_feedback("nope") == "negative"


def test_none_write_a_function_long_imperative():
    assert feedback.classify_feedback(
        "write a function that returns an error message"
    ) is None


def test_none_question_how_do_i_handle_errors():
    assert feedback.classify_feedback("how do I handle errors") is None


def test_none_imperative_explain():
    assert feedback.classify_feedback("explain what went wrong") is None


def test_none_empty():
    assert feedback.classify_feedback("") is None


def test_none_imperative_add_works_method():
    assert feedback.classify_feedback("add a works() method") is None


def test_none_long_sentence_containing_thanks():
    assert feedback.classify_feedback(
        "thanks for the help, now can you also add retry logic to the client"
    ) is None


def test_none_no_text():
    assert feedback.classify_feedback(None) is None
