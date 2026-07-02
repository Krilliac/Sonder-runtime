import training_tasks


def test_all_tasks_well_formed_and_unique_names():
    assert len(training_tasks.TASKS) >= 30
    names = set()
    for t in training_tasks.TASKS:
        assert t["name"].strip()
        assert t["prompt"].strip()
        assert t["check"].strip()
        names.add(t["name"])
    assert len(names) == len(training_tasks.TASKS)


def test_sample_returns_distinct_tasks():
    picked = training_tasks.sample(3)
    assert len(picked) == 3
    names = {t["name"] for t in picked}
    assert len(names) == 3


def test_sample_caps_at_pool_size():
    picked = training_tasks.sample(9999)
    assert len(picked) == len(training_tasks.TASKS)
