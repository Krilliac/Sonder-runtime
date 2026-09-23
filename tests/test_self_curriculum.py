import json

import curriculum_store
import self_curriculum
import training_tasks

VALID_TASK = {
    "name": "reverse_words",
    "prompt": "Write a Python function named `reverse_words(s)` that reverses word order. "
              "Return ONLY the function in one python code block.",
    "check": "assert reverse_words('a b c') == 'c b a'\nassert reverse_words('') == ''",
    "reference": "def reverse_words(s):\n    return ' '.join(reversed(s.split()))",
}


def _stub_ok(*_a, **_k):
    return True, ""


def _stub_fail(*_a, **_k):
    return False, "err"


# ---------- parse_task ----------

def test_parse_task_parses_fenced_json():
    text = "here's the task:\n```json\n%s\n```\nenjoy" % json.dumps(VALID_TASK)
    parsed = self_curriculum.parse_task(text)
    assert parsed == VALID_TASK


def test_parse_task_parses_inline_json():
    text = "prose before %s prose after" % json.dumps(VALID_TASK)
    parsed = self_curriculum.parse_task(text)
    assert parsed == VALID_TASK


def test_parse_task_none_on_garbage():
    assert self_curriculum.parse_task("no json here at all") is None
    assert self_curriculum.parse_task("") is None
    assert self_curriculum.parse_task(None) is None


def test_parse_task_none_on_missing_keys():
    incomplete = {"name": "foo", "prompt": "do a thing"}
    text = json.dumps(incomplete)
    assert self_curriculum.parse_task(text) is None


def test_parse_task_none_on_malformed_json():
    text = "{ this is not: valid json ]"
    assert self_curriculum.parse_task(text) is None


def test_malformed_prompt_is_rejected_without_crashing_task_validation():
    task = dict(VALID_TASK, prompt=None)
    parsed = self_curriculum.parse_task(json.dumps(task))
    assert self_curriculum.is_valid(parsed, run_code_fn=_stub_ok) is False


# ---------- is_valid ----------

def test_is_valid_true_with_assert_and_passing_reference():
    assert self_curriculum.is_valid(VALID_TASK, run_code_fn=_stub_ok) is True


def test_is_valid_false_when_check_has_no_assert():
    task = dict(VALID_TASK, check="print(True)")
    assert self_curriculum.is_valid(task, run_code_fn=_stub_ok) is False


def test_is_valid_false_when_run_code_fails():
    assert self_curriculum.is_valid(VALID_TASK, run_code_fn=_stub_fail) is False


def test_is_valid_false_when_missing_key():
    task = {k: v for k, v in VALID_TASK.items() if k != "reference"}
    assert self_curriculum.is_valid(task, run_code_fn=_stub_ok) is False


def test_is_valid_false_when_key_is_empty_string():
    task = dict(VALID_TASK, name="   ")
    assert self_curriculum.is_valid(task, run_code_fn=_stub_ok) is False


def test_is_valid_false_on_none_task():
    assert self_curriculum.is_valid(None, run_code_fn=_stub_ok) is False


# ---------- is_novel ----------

def test_is_novel_false_when_name_exists():
    assert self_curriculum.is_novel(VALID_TASK, {"reverse_words", "factorial"}) is False


def test_is_novel_true_when_name_new():
    assert self_curriculum.is_novel(VALID_TASK, {"factorial"}) is True


# ---------- generate_one ----------

def test_generate_one_parses_gen_fn_output():
    gen_fn = lambda: json.dumps(VALID_TASK)
    assert self_curriculum.generate_one(gen_fn) == VALID_TASK


def test_generate_one_none_on_garbage():
    gen_fn = lambda: "not json"
    assert self_curriculum.generate_one(gen_fn) is None


# ---------- harvest ----------

def test_harvest_collects_n_accepted():
    other_task = dict(VALID_TASK, name="other_task")
    outputs = [json.dumps(VALID_TASK), json.dumps(other_task)]
    calls = {"i": 0}

    def gen_fn():
        text = outputs[calls["i"] % len(outputs)]
        calls["i"] += 1
        return text

    accepted = self_curriculum.harvest(2, gen_fn, existing_names=set(), run_code_fn=_stub_ok)
    assert len(accepted) == 2
    names = {t["name"] for t in accepted}
    assert names == {"reverse_words", "other_task"}


def test_harvest_capped_by_max_attempts_on_duplicates_and_garbage():
    outputs = [json.dumps(VALID_TASK), "garbage", json.dumps(VALID_TASK)]
    calls = {"i": 0}

    def gen_fn():
        text = outputs[calls["i"] % len(outputs)]
        calls["i"] += 1
        return text

    # Only ever one distinct valid+novel task available; ask for 5 but cap attempts low.
    accepted = self_curriculum.harvest(
        5, gen_fn, existing_names=set(), run_code_fn=_stub_ok, max_attempts=6
    )
    assert len(accepted) == 1
    assert calls["i"] == 6


def test_harvest_skips_already_known_names():
    accepted = self_curriculum.harvest(
        3, lambda: json.dumps(VALID_TASK), existing_names={"reverse_words"},
        run_code_fn=_stub_ok, max_attempts=4,
    )
    assert accepted == []


def test_harvest_default_max_attempts_is_n_times_4():
    calls = {"i": 0}

    def gen_fn():
        calls["i"] += 1
        return "garbage"

    accepted = self_curriculum.harvest(3, gen_fn, existing_names=set(), run_code_fn=_stub_ok)
    assert accepted == []
    assert calls["i"] == 12


# ---------- curriculum_store ----------

def test_store_append_then_load_round_trips(tmp_path):
    path = tmp_path / "generated_tasks.jsonl"
    assert curriculum_store.load(path) == []
    curriculum_store.append([VALID_TASK], path)
    loaded = curriculum_store.load(path)
    assert loaded == [VALID_TASK]


def test_store_append_is_additive(tmp_path):
    path = tmp_path / "generated_tasks.jsonl"
    other_task = dict(VALID_TASK, name="other_task")
    curriculum_store.append([VALID_TASK], path)
    curriculum_store.append([other_task], path)
    loaded = curriculum_store.load(path)
    assert [t["name"] for t in loaded] == ["reverse_words", "other_task"]


def test_store_names_unions_with_training_tasks(tmp_path):
    path = tmp_path / "generated_tasks.jsonl"
    curriculum_store.append([VALID_TASK], path)
    result = curriculum_store.names(path)
    assert "reverse_words" in result
    assert {t["name"] for t in training_tasks.TASKS} <= result


def test_store_load_missing_file_returns_empty(tmp_path):
    path = tmp_path / "does_not_exist.jsonl"
    assert curriculum_store.load(path) == []
