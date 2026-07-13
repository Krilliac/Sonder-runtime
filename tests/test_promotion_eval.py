import json
import sqlite3
from urllib.error import URLError

import pytest

import eval_models
import promotion_eval as pe


ANSWERS = {
    "completed_revenue_by_region": (
        "SELECT region, SUM(amount) AS total_revenue FROM orders "
        "WHERE status = 'completed' GROUP BY region ORDER BY region ASC"
    ),
    "repeat_paid_customers": (
        "SELECT c.customer_name, COUNT(*) AS paid_count, SUM(p.amount) AS paid_total "
        "FROM customers c JOIN payments p ON p.customer_id = c.customer_id "
        "WHERE p.status = 'paid' GROUP BY c.customer_id, c.customer_name "
        "HAVING COUNT(*) >= 2 ORDER BY paid_total DESC, customer_name ASC"
    ),
    "latest_sensor_reading": (
        "SELECT r.sensor_id, r.observed_at, r.value FROM readings r "
        "WHERE r.observed_at = (SELECT MAX(r2.observed_at) FROM readings r2 "
        "WHERE r2.sensor_id = r.sensor_id) ORDER BY r.sensor_id ASC"
    ),
    "products_without_sales": (
        "SELECT p.product_id, p.product_name FROM products p "
        "WHERE NOT EXISTS (SELECT 1 FROM sale_items s WHERE s.product_id = p.product_id) "
        "ORDER BY p.product_id ASC"
    ),
}


def _task_id(prompt):
    for task in pe.TASKS:
        if task.request in prompt:
            return task.task_id
    raise AssertionError("unknown prompt")


def _perfect_generate(model, prompt, *, options, timeout):
    del model, options, timeout
    return ANSWERS[_task_id(prompt)]


def test_evaluate_model_passes_raw_and_exact_sql_fence():
    def generate(model, prompt, *, options, timeout):
        answer = _perfect_generate(model, prompt, options=options, timeout=timeout)
        if _task_id(prompt) == "latest_sensor_reading":
            return f"```sql\n{answer}\n```"
        return answer

    report = pe.evaluate_model("exact-model:sha", generate=generate)

    assert report["score"] == 4
    assert report["total"] == 4
    assert all(task["reason"] == "passed" for task in report["tasks"])
    assert all(len(task["artifact_sha256"]) == 64 for task in report["tasks"])


def test_wrong_answer_marker_and_prose_all_fail_without_leaking_output():
    responses = {
        "completed_revenue_by_region": "SELECT region, COUNT(*) AS total_revenue FROM orders GROUP BY region ORDER BY region",
        "repeat_paid_customers": "SONDER_VALID",
        "latest_sensor_reading": "Here is the query: SELECT * FROM readings",
        "products_without_sales": "```sql\nSELECT product_id, product_name FROM products ORDER BY product_id\n```\nextra",
    }

    def generate(_model, prompt, **_kwargs):
        return responses[_task_id(prompt)]

    report = pe.evaluate_model("candidate:bad", generate=generate)
    reasons = {task["id"]: task["reason"] for task in report["tasks"]}
    assert report["score"] == 0
    assert reasons == {
        "completed_revenue_by_region": "wrong_result",
        "repeat_paid_customers": "not_read_only_query",
        "latest_sensor_reading": "not_read_only_query",
        "products_without_sales": "invalid_fence",
    }
    serialized = json.dumps(report)
    assert "SONDER_VALID" not in serialized
    assert "Here is the query" not in serialized
    assert "SELECT" not in serialized


def test_hidden_fixtures_reject_status_and_grouping_shortcuts():
    revenue_shortcut = (
        "SELECT region, SUM(amount) AS total_revenue FROM orders "
        "WHERE status NOT IN ('pending','cancelled') GROUP BY region ORDER BY region"
    )
    customer_shortcut = (
        "SELECT c.customer_name, COUNT(DISTINCT p.amount) AS paid_count, "
        "SUM(p.amount) AS paid_total FROM customers c JOIN payments p "
        "ON p.customer_id=c.customer_id WHERE p.status='paid' "
        "GROUP BY c.customer_name HAVING COUNT(DISTINCT p.amount)>=2 "
        "ORDER BY paid_total DESC, customer_name ASC"
    )
    assert pe._evaluate_task(revenue_shortcut, pe.TASKS[0])["reason"] == "wrong_result"
    assert pe._evaluate_task(customer_shortcut, pe.TASKS[1])["reason"] == "wrong_result"


def test_unsafe_queries_are_denied_even_when_hidden_in_with_clause():
    task = pe.TASKS[0]
    unsafe = [
        "DELETE FROM orders",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE ':memory:' AS other",
        "WITH gone AS (DELETE FROM orders RETURNING *) SELECT * FROM gone",
        "WITH x AS (SELECT load_extension('evil')) SELECT * FROM x",
        "SELECT writefile('x', 'bad') AS region, 1 AS total_revenue",
    ]
    for query in unsafe:
        result = pe._evaluate_task(query, task)
        assert not result["passed"], query
        assert result["reason"] in {"not_read_only_query", "sql_error", "unsafe_sql"}


def test_multiple_statements_and_comments_cannot_smuggle_mutation():
    task = pe.TASKS[0]
    query = ANSWERS[task.task_id] + "; DELETE FROM orders"
    result = pe._evaluate_task(query, task)
    assert not result["passed"]
    assert result["reason"] == "sql_error"

    commented = "SELECT region, SUM(amount) AS total_revenue FROM orders -- hidden\nGROUP BY region"
    assert pe._evaluate_task(commented, task)["reason"] == "comments_not_allowed"


def test_vm_result_and_sql_budgets_are_enforced(monkeypatch):
    task = pe.TASKS[0]
    monkeypatch.setattr(pe, "MAX_VM_STEPS", 100)
    expensive = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x < 100000) "
        "SELECT 'east' AS region, SUM(x) AS total_revenue FROM n"
    )
    assert pe._evaluate_task(expensive, task)["reason"] == "vm_step_limit"

    monkeypatch.setattr(pe, "MAX_RESULT_ROWS", 1)
    many_rows = "SELECT region, amount AS total_revenue FROM orders ORDER BY region"
    assert pe._evaluate_task(many_rows, task)["reason"] == "result_row_limit"

    too_long = "SELECT '" + ("a" * pe.MAX_SQL_BYTES) + "'"
    assert pe._evaluate_task(too_long, task)["reason"] == "sql_too_large"

    value_bomb = (
        "WITH RECURSIVE grow(x) AS (SELECT 'xx' UNION ALL SELECT x||x FROM grow "
        "WHERE length(x) < 1000000) SELECT 'east' AS region, max(x) AS total_revenue FROM grow"
    )
    assert pe._evaluate_task(value_bomb, task)["reason"] in {"sql_error", "vm_step_limit"}


def test_fixture_authorizer_denies_catalog_and_unknown_table_reads():
    task = pe.TASKS[0]
    catalog = "SELECT name AS region, 1 AS total_revenue FROM sqlite_master"
    assert pe._evaluate_task(catalog, task)["reason"] == "unsafe_sql"


def test_generation_failures_are_bounded_and_do_not_abort_suite():
    calls = 0

    def generate(model, prompt, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("secret backend detail")
        return _perfect_generate(model, prompt, **kwargs)

    report = pe.evaluate_model("model:v1", generate=generate)
    assert report["score"] == 3
    assert report["tasks"][0] == {
        "id": "completed_revenue_by_region",
        "passed": False,
        "reason": "generation_error",
        "artifact_sha256": None,
    }
    assert "secret backend detail" not in json.dumps(report)


def test_evaluate_pair_is_sequential_and_uses_fixed_options():
    calls = []

    def generate(model, prompt, *, options, timeout):
        calls.append((model, _task_id(prompt), options, timeout))
        options["seed"] = -1  # Mutating the per-call copy must not change the constant.
        return ANSWERS[_task_id(prompt)]

    report = pe.evaluate_pair("base@digest", "candidate@digest", generate=generate)
    assert [call[0] for call in calls] == ["base@digest"] * 4 + ["candidate@digest"] * 4
    assert all(call[2] == {**pe.INFERENCE_OPTIONS, "seed": -1} for call in calls)
    assert all(call[3] == pe.INFERENCE_TIMEOUT_SECONDS for call in calls)
    assert report["options"] == pe.INFERENCE_OPTIONS
    assert pe.INFERENCE_OPTIONS == {
        "temperature": 0,
        "seed": 817_263,
        "num_ctx": 2_048,
        "num_predict": 256,
    }


def _decision_report(base_passes, candidate_passes):
    def model_report(prefix, passes):
        tasks = [
            {"id": task.task_id, "passed": index in passes,
             "reason": "passed" if index in passes else "wrong_result",
             "artifact_sha256": "0" * 64}
            for index, task in enumerate(pe.TASKS)
        ]
        return {"model": prefix, "score": len(passes), "total": 4, "tasks": tasks}

    return {
        "schema": pe.REPORT_SCHEMA,
        "suite_version": pe.SUITE_VERSION,
        "suite_hash": pe.SUITE_HASH,
        "challenge_hash": __import__("hashlib").sha256(b"").hexdigest(),
        "options": dict(pe.INFERENCE_OPTIONS),
        "base": model_report("base", set(base_passes)),
        "candidate": model_report("candidate", set(candidate_passes)),
    }


def test_promotion_decision_requires_floor_lift_and_no_regression():
    assert pe.promotion_decision(_decision_report({0, 1}, {0, 1, 2})) == (True, "promotion_passed")
    assert pe.promotion_decision(_decision_report({0, 1, 2}, {0, 1, 2, 3})) == (True, "promotion_passed")
    assert pe.promotion_decision(_decision_report({0, 1, 2, 3}, {0, 1, 2, 3})) == (True, "promotion_passed")

    assert pe.promotion_decision(_decision_report({0, 1}, {0, 1}))[1] == "candidate_below_floor"
    assert pe.promotion_decision(_decision_report({0, 1, 2}, {0, 1, 2}))[1] == "candidate_has_no_lift"
    accepted, reason = pe.promotion_decision(_decision_report({0, 1, 2}, {0, 1, 3}))
    assert not accepted
    assert reason.startswith("task_regression:")


def test_promotion_decision_rejects_tampering_and_partial_runs():
    assert pe.promotion_decision([]) == (False, "invalid_report")

    report = _decision_report({0, 1, 2, 3}, {0, 1, 2, 3})
    report["suite_hash"] = "tampered"
    assert pe.promotion_decision(report) == (False, "suite_mismatch")

    report = _decision_report({0, 1}, {0, 1, 2})
    report["candidate"]["total"] = 3
    assert pe.promotion_decision(report) == (False, "incomplete_suite")

    report = _decision_report({0, 1}, {0, 1, 2})
    report["candidate"]["tasks"][3]["passed"] = "false"
    assert pe.promotion_decision(report) == (False, "invalid_report")

    report = _decision_report({0, 1}, {0, 1, 2})
    report["candidate"]["score"] = 4
    assert pe.promotion_decision(report) == (False, "invalid_report")

    report = _decision_report({0, 1}, {0, 1, 2})
    report["base"] = []
    assert pe.promotion_decision(report) == (False, "invalid_report")

    report = _decision_report({0, 1}, {0, 1, 2})
    report["base"]["tasks"][2].update(reason="generation_error", artifact_sha256=None)
    assert pe.promotion_decision(report) == (False, "evaluation_infrastructure_error")

    report = _decision_report({0, 1}, {0, 1, 2})
    assert pe.promotion_decision(
        report, expected_base="other", expected_candidate="candidate"
    ) == (False, "model_mismatch")

    report = _decision_report({0, 1}, {0, 1, 2})
    assert pe.promotion_decision(
        report, expected_challenge="different"
    ) == (False, "challenge_mismatch")


def test_challenged_promotion_requires_instruction_probe_and_three_sql_passes():
    challenge = "decision-challenge"

    def challenged(base_pass, candidate_pass, candidate_instruction=True):
        report = _decision_report(base_pass, candidate_pass)
        report["challenge_hash"] = __import__("hashlib").sha256(challenge.encode()).hexdigest()
        for model_report, passed in (
            (report["base"], False),
            (report["candidate"], candidate_instruction),
        ):
            model_report["tasks"].append({
                "id": pe.STRUCTURED_TASK_ID,
                "passed": passed,
                "reason": "passed" if passed else "wrong_result",
                "artifact_sha256": "f" * 64,
            })
            model_report["score"] += int(passed)
            model_report["total"] += 1
        return report

    accepted = challenged({0, 3}, {0, 1, 3})
    assert pe.promotion_decision(accepted, expected_challenge=challenge) == (
        True, "promotion_passed"
    )
    no_instruction = challenged({0, 3}, {0, 1, 3}, candidate_instruction=False)
    assert pe.promotion_decision(no_instruction, expected_challenge=challenge)[1] == (
        "candidate_failed_instruction_probe"
    )
    only_two_sql = challenged({0}, {0, 3})
    assert pe.promotion_decision(only_two_sql, expected_challenge=challenge)[1] == (
        "candidate_below_floor"
    )


def test_standalone_final_report_rejects_infrastructure_failure():
    report = _decision_report({0, 1, 3}, {0, 1, 3})["candidate"]
    report["model"] = "sonder-personal:latest"
    report["tasks"].append({
        "id": pe.STRUCTURED_TASK_ID,
        "passed": True,
        "reason": "passed",
        "artifact_sha256": "f" * 64,
    })
    report["score"] += 1
    report["total"] += 1
    report["tasks"][2].update(
        passed=False, reason="generation_error", artifact_sha256=None,
    )

    assert pe.validate_model_report(
        report,
        expected_model="sonder-personal:latest",
        challenge="deployment",
    ) == (False, "evaluation_infrastructure_error")


def test_task_selection_validation_and_exact_model_name():
    subset = pe.evaluate_model(
        "model:one",
        generate=_perfect_generate,
        task_ids=["products_without_sales"],
    )
    assert subset["score"] == subset["total"] == 1
    assert subset["tasks"][0]["id"] == "products_without_sales"

    for model in ("", " model", "model "):
        try:
            pe.evaluate_model(model, generate=_perfect_generate)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid model name accepted")

    try:
        pe.evaluate_model("model", generate=_perfect_generate, task_ids=["missing"])
    except ValueError:
        pass
    else:
        raise AssertionError("unknown task accepted")


def test_deployment_challenge_randomizes_table_identifiers():
    prompts = []

    def generate(model, prompt, **_kwargs):
        prompts.append(prompt)
        return "SELECT 1"

    report = pe.evaluate_model("model", generate=generate, challenge="deployment-one")
    assert report["score"] == 0
    assert all("_v" in prompt for prompt in prompts[:4])
    assert "nonce" in prompts[4]
    assert "orders_v" in prompts[0]
    assert "customers_v" in prompts[1]
    assert pe._variant_tasks("deployment-one")[0].schema_for_prompt != pe._variant_tasks("deployment-two")[0].schema_for_prompt


def test_dynamic_instruction_probe_is_host_graded_and_nonce_bound():
    challenge = "deployment-instruction"
    _prompt, expected = pe._structured_challenge(challenge)
    report = pe.evaluate_model(
        "model",
        challenge=challenge,
        task_ids=[pe.STRUCTURED_TASK_ID],
        generate=lambda *args, **kwargs: json.dumps(expected),
    )
    assert report["score"] == report["total"] == 1

    wrong = pe.evaluate_model(
        "model",
        challenge=challenge,
        task_ids=[pe.STRUCTURED_TASK_ID],
        generate=lambda *args, **kwargs: json.dumps({**expected, "nonce": "stale"}),
    )
    assert wrong["score"] == 0

    duplicate = '{"nonce":"x","nonce":"y","uppercase":"A","reversed":"a","sum":1}'
    rejected = pe._evaluate_structured(duplicate, expected)
    assert not rejected["passed"] and rejected["reason"] == "invalid_json"


def test_authorizer_blocks_nonallowlisted_functions_directly():
    connection = sqlite3.connect(":memory:")
    try:
        connection.set_authorizer(pe._authorizer)
        try:
            connection.execute("SELECT random()")
        except sqlite3.DatabaseError as error:
            assert "authorized" in str(error).lower()
        else:
            raise AssertionError("random() was allowed")
        assert connection.execute("SELECT upper('ok')").fetchone() == ("OK",)
    finally:
        connection.close()


def test_suite_hash_is_stable_length_and_reports_never_contain_hidden_rows():
    report = pe.evaluate_model("model", generate=_perfect_generate)
    assert len(pe.SUITE_HASH) == 64
    serialized = json.dumps(report)
    for hidden_value in ("anvil", "brush", "Iris", "2026-06-10T01:00:01Z"):
        assert hidden_value not in serialized
    assert set(report["tasks"][0]) == {"id", "passed", "reason", "artifact_sha256"}


def test_default_generator_posts_fixed_nondeterminism_controls(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            captured["read_limit"] = limit
            return json.dumps({"response": "SELECT 1"}).encode()

    def open_local(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    monkeypatch.setattr(pe, "_open_local", open_local)
    result = pe._default_generate(
        "model@digest",
        "prompt",
        options=dict(pe.INFERENCE_OPTIONS),
        timeout=pe.INFERENCE_TIMEOUT_SECONDS,
    )

    assert result == "SELECT 1"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["body"] == {
        "model": "model@digest",
        "prompt": "prompt",
        "stream": False,
        "options": pe.INFERENCE_OPTIONS,
    }
    assert captured["timeout"] == pe.INFERENCE_TIMEOUT_SECONDS
    assert captured["read_limit"] == pe.MAX_MODEL_RESPONSE_BYTES + 1


def test_default_generator_rejects_non_loopback_host(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://models.example.test:11434")
    try:
        pe._default_generate("model", "prompt", options=dict(pe.INFERENCE_OPTIONS), timeout=1)
    except ValueError as error:
        assert "loopback" in str(error)
    except URLError as error:  # pragma: no cover - proves no network attempt should occur
        raise AssertionError("non-loopback network request attempted") from error
    else:
        raise AssertionError("non-loopback Ollama host accepted")


def test_promotion_origin_rewrites_ipv6_bind_all_and_ignores_remote_opt_in(
    monkeypatch,
):
    monkeypatch.setenv("OLLAMA_HOST", "[::]:11434")
    assert pe._local_ollama_origin() == "http://[::1]:11434"

    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.test:11434")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    with pytest.raises(ValueError, match="loopback"):
        pe._local_ollama_origin()


def test_local_opener_disables_proxies_and_redirects(monkeypatch):
    captured = {}

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return "response"

    def build_opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(pe.urllib.request, "build_opener", build_opener)
    request = pe.urllib.request.Request("http://127.0.0.1:11434/api/generate")
    assert pe._open_local(request, 12) == "response"
    proxy = next(handler for handler in captured["handlers"] if isinstance(handler, pe.urllib.request.ProxyHandler))
    assert proxy.proxies == {}
    assert any(isinstance(handler, pe._NoRedirect) for handler in captured["handlers"])


def test_local_model_digest_requires_exact_alias_and_bounded_hash(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({
                "models": [
                    {"name": "candidate:exact", "digest": "a" * 64},
                    {"name": "candidate:other", "digest": "b" * 64},
                ]
            }).encode()

    monkeypatch.setattr(pe, "_open_local", lambda request, timeout: Response())
    assert pe.local_model_digest("candidate:exact") == "a" * 64
    try:
        pe.local_model_digest("candidate")
    except ValueError as error:
        assert "not installed" in str(error)
    else:
        raise AssertionError("partial model alias matched")


def test_default_evaluation_unloads_model_after_the_suite(monkeypatch):
    generated = []
    unloaded = []
    monkeypatch.setattr(
        pe, "_default_generate",
        lambda model, prompt, **kwargs: (
            generated.append((model, _task_id(prompt)))
            or ANSWERS[_task_id(prompt)]
        ),
    )
    monkeypatch.setattr(pe, "_default_unload", lambda model: unloaded.append(model))

    report = pe.evaluate_model("candidate:exact")

    assert report["score"] == 4
    assert len(generated) == 4
    assert unloaded == ["candidate:exact"]


def test_eval_cli_forwards_exact_models_challenge_and_exit_code(monkeypatch, capsys):
    seen = {}
    report = {"schema": pe.REPORT_SCHEMA}

    def evaluate_pair(base, candidate, *, challenge):
        seen.update(base=base, candidate=candidate, challenge=challenge)
        return report

    def decision(value, **expected):
        assert value is report
        seen["expected"] = expected
        return False, "rejected_for_test"

    monkeypatch.setattr(eval_models.promotion_eval, "evaluate_pair", evaluate_pair)
    monkeypatch.setattr(eval_models.promotion_eval, "promotion_decision", decision)
    assert eval_models.main(["base:exact", "candidate:exact", "--challenge", "nonce-1"]) == 1
    assert seen == {
        "base": "base:exact",
        "candidate": "candidate:exact",
        "challenge": "nonce-1",
        "expected": {
            "expected_base": "base:exact",
            "expected_candidate": "candidate:exact",
            "expected_challenge": "nonce-1",
        },
    }
    assert json.loads(capsys.readouterr().out)["reason"] == "rejected_for_test"
