import reward


def test_known_signals_score():
    assert reward.score("tests_passed") == 1.0
    assert reward.score("used") == 0.9
    assert reward.score("copied") == 0.85
    assert reward.score("edited") == 0.75
    assert reward.score("failed") == -1.0


def test_unknown_signal_is_zero():
    assert reward.score("banana") == 0.0


def test_is_good_threshold():
    assert reward.is_good("tests_passed") is True
    assert reward.is_good("compiled") is True   # 0.7, at threshold
    assert reward.is_good("rejected") is False


def test_valid_signals_set():
    assert "accepted" in reward.VALID_SIGNALS
    assert "copied" in reward.VALID_SIGNALS
    assert "banana" not in reward.VALID_SIGNALS
