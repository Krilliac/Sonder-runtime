import sonder_repl
import sonder_serve


def test_repl_train_cap_is_much_larger(monkeypatch):
    monkeypatch.setattr(sonder_repl, "TRAIN_MAX_N", 500)

    assert sonder_repl._parse_train_n("9999") == 500


def test_serve_train_cap_is_much_larger(monkeypatch):
    monkeypatch.setattr(sonder_serve, "TRAIN_MAX_N", 500)

    assert sonder_serve._parse_train_n("9999") == (500, None)


def test_serve_learn_alias_uses_grounded_practice(monkeypatch):
    monkeypatch.setattr(sonder_serve, "_do_train", lambda n: "practiced %d" % n)

    assert sonder_serve._handle_slash("/learn 7") == "practiced 7"
