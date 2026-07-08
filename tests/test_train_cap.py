import trilobite_repl
import trilobite_serve


def test_repl_train_cap_is_much_larger(monkeypatch):
    monkeypatch.setattr(trilobite_repl, "TRAIN_MAX_N", 500)

    assert trilobite_repl._parse_train_n("9999") == 500


def test_serve_train_cap_is_much_larger(monkeypatch):
    monkeypatch.setattr(trilobite_serve, "TRAIN_MAX_N", 500)

    assert trilobite_serve._parse_train_n("9999") == (500, None)


def test_serve_learn_alias_uses_training(monkeypatch):
    monkeypatch.setattr(trilobite_serve, "_do_train", lambda n: "trained %d" % n)

    assert trilobite_serve._handle_slash("/learn 7") == "trained 7"
