"""Context-overflow classification and the exactly-one compaction retry."""
import io
import urllib.error

import pytest

import context_overflow
import server


# --- classifier: bounded normalization --------------------------------------


def test_normalization_is_bounded_to_4096_utf8_bytes():
    text, truncated = context_overflow.normalize("a" * 5000)

    assert truncated is True
    assert len(text.encode("utf-8")) <= context_overflow.NORMALIZE_MAX_BYTES


def test_evidence_past_the_normalization_bound_is_not_matched():
    padded = "x" * context_overflow.NORMALIZE_MAX_BYTES + " context length exceeded"

    verdict = context_overflow.classify(padded, status=400)

    assert verdict.overflow is False
    assert verdict.truncated is True


def test_evidence_inside_the_normalization_bound_still_matches():
    padded = "x" * 64 + " context length exceeded " + "y" * 8000

    verdict = context_overflow.classify(padded, status=400)

    assert verdict.overflow is True
    assert verdict.truncated is True


def test_multibyte_text_is_clipped_on_a_codepoint_boundary():
    # A 3-byte codepoint repeated past the budget: the cut lands mid-character.
    text, truncated = context_overflow.normalize("中" * 2000)

    assert truncated is True
    assert len(text.encode("utf-8")) <= context_overflow.NORMALIZE_MAX_BYTES


@pytest.mark.parametrize("value", [None, "", 42, {"error": "unrelated"}, b"bytes"])
def test_non_text_input_is_safe_and_conservative(value):
    verdict = context_overflow.classify(value)

    assert verdict.overflow is False


# --- classifier: positive evidence ------------------------------------------


@pytest.mark.parametrize("detail", [
    "context_length_exceeded",
    "Context Length Exceeded.",
    "the input exceeds context window",
    "trying to keep the first tokens when context is full",
    "prompt is too long: 5000 tokens",
    "Please reduce the length of the messages.",
    "requested tokens exceed context window size",
])
def test_exact_overflow_phrases_classify(detail):
    verdict = context_overflow.classify(detail, status=400)

    assert verdict.overflow is True
    assert verdict.reason == context_overflow.REASON_CONTEXT_PHRASE
    assert verdict.evidence


@pytest.mark.parametrize("detail", [
    "This model's maximum context length is 8192 tokens, however you requested 9000",
    "maximum context length is 8192 tokens; your messages resulted in 9000 tokens",
    "max context window of 4096 exceeded",
    "requested 9000 tokens but the model context is only 8192",
    "12000 tokens exceeds the model context",
    "n_ctx (4096) is less than n_tokens (5000)",
])
def test_bounded_maximum_context_patterns_classify(detail):
    verdict = context_overflow.classify(detail, status=400)

    assert verdict.overflow is True
    assert verdict.reason == context_overflow.REASON_CONTEXT_LIMIT
    assert verdict.evidence


# --- classifier: status is supporting evidence only -------------------------


@pytest.mark.parametrize("status", [400, 413, 422, 429, 500])
def test_status_alone_never_classifies_an_overflow(status):
    verdict = context_overflow.classify("upstream connection reset", status=status)

    assert verdict.overflow is False
    assert verdict.status == status


def test_overflow_phrase_wins_under_a_misleading_rate_limit_status():
    verdict = context_overflow.classify(
        "context length exceeded for this request", status=429,
    )

    assert verdict.overflow is True
    assert verdict.status == 429


def test_overflow_phrase_wins_over_a_co_reported_rate_limit():
    verdict = context_overflow.classify(
        "rate limit hit; maximum context length is 8192 tokens, however you requested 9000",
        status=429,
    )

    assert verdict.overflow is True
    assert verdict.control == context_overflow.CONTROL_RATE_LIMIT


def test_malformed_status_is_ignored_rather_than_fatal():
    verdict = context_overflow.classify("context length exceeded", status="nope")

    assert verdict.overflow is True
    assert verdict.status is None


@pytest.mark.parametrize("detail", [
    "this is not a context length exceeded failure",
    'diagnostic says "context_length_exceeded": false',
    "the literal phrase context length exceeded is forbidden in this field",
    "an example of context length exceeded is shown in the documentation",
])
def test_negated_or_meta_mentions_do_not_authorize_a_retry(detail):
    assert context_overflow.classify(detail, status=400).overflow is False


@pytest.mark.parametrize("detail", [
    "This model's maximum context length is 8192 tokens",
    "requested 100 tokens but the model context is 8192",
    "maximum context length is 8192 tokens, however you requested 100",
])
def test_limit_mentions_without_a_demonstrated_overflow_are_conservative(detail):
    assert context_overflow.classify(detail, status=400).overflow is False


# --- classifier: negative controls ------------------------------------------


@pytest.mark.parametrize("detail,control", [
    ("Request Entity Too Large", context_overflow.CONTROL_BODY_TOO_LARGE),
    ("payload too large", context_overflow.CONTROL_BODY_TOO_LARGE),
    ("CUDA out of memory", context_overflow.CONTROL_DEVICE_OUT_OF_MEMORY),
    ("unable to allocate backend buffer", context_overflow.CONTROL_DEVICE_OUT_OF_MEMORY),
    ("Rate limit reached for this model", context_overflow.CONTROL_RATE_LIMIT),
    ("too many requests", context_overflow.CONTROL_RATE_LIMIT),
    ('model "coder" not found, try pulling it first', context_overflow.CONTROL_MODEL_MISSING),
    ("no such model", context_overflow.CONTROL_MODEL_MISSING),
])
def test_negative_controls_are_named_and_not_overflow(detail, control):
    verdict = context_overflow.classify(detail, status=400)

    assert verdict.overflow is False
    assert verdict.control == control


@pytest.mark.parametrize("prefix", [
    "Request Entity Too Large",
    "CUDA out of memory",
    "model not found",
])
def test_vetoing_controls_override_overflow_wording(prefix):
    verdict = context_overflow.classify(
        prefix + "; maximum context length of 8192 was exceeded", status=400,
    )

    assert verdict.overflow is False
    assert verdict.vetoed is True


# --- bounded compaction -----------------------------------------------------


def _conversation(turns):
    messages = [{"role": "system", "content": "be terse"}]
    for i in range(turns):
        messages.append({"role": "user", "content": "q%d" % i})
        messages.append({"role": "assistant", "content": "a%d" % i})
    messages.append({"role": "user", "content": "live request"})
    return messages


def test_compaction_keeps_system_preamble_and_the_live_request():
    compacted = context_overflow.compact_messages(_conversation(4))

    assert compacted[0] == {"role": "system", "content": "be terse"}
    assert compacted[1]["content"] == context_overflow.COMPACTION_NOTE
    assert compacted[-1] == {"role": "user", "content": "live request"}
    assert len(compacted) < len(_conversation(4))


def test_compaction_drops_the_oldest_turns_and_keeps_content_verbatim():
    compacted = context_overflow.compact_messages(_conversation(4))
    kept = [m["content"] for m in compacted]

    assert "q0" not in kept
    assert "q3" in kept and "a3" in kept
    # Nothing that survives is rewritten or truncated.
    assert all(len(m["content"]) < 200 for m in compacted[2:])


def test_compaction_never_orphans_tool_results_or_assistant_messages():
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old response"},
        {"role": "user", "content": "newer request"},
        {"role": "assistant", "content": "calling tool", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "tool result"},
        {"role": "assistant", "content": "newer response"},
        {"role": "user", "content": "live request"},
    ]

    compacted = context_overflow.compact_messages(messages)

    assert compacted[2]["role"] == "user"
    assert compacted[2]["content"] == "newer request"
    assert compacted[-1] == {"role": "user", "content": "live request"}


@pytest.mark.parametrize("keep_recent", ["bad", object()])
def test_malformed_keep_recent_is_conservative(keep_recent):
    assert context_overflow.compact_messages(
        _conversation(4), keep_recent=keep_recent,
    ) is None


def test_a_single_oversized_request_is_uncompactable():
    # System + one user turn: there is no history to drop, and truncating the
    # request itself would silently corrupt it.
    assert context_overflow.compact_messages([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "x" * 10000},
    ]) is None


@pytest.mark.parametrize("messages", [None, "not a list", [], [{"role": "user"}]])
def test_uncompactable_shapes_return_none(messages):
    assert context_overflow.compact_messages(messages) is None


def test_compaction_is_not_applied_twice():
    once = context_overflow.compact_messages(_conversation(6))

    assert once is not None
    assert context_overflow.compact_messages(once) is None


# --- gateway: exactly-one compaction retry ----------------------------------


def _http_error(code, body):
    return urllib.error.HTTPError(
        server.BASE + "/api/chat", code, "failure", {}, io.BytesIO(body),
    )


def _payload(turns=4, num_ctx=8192):
    return {
        "model": "local",
        "messages": _conversation(turns),
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 256, "num_ctx": num_ctx},
    }


@pytest.fixture(autouse=True)
def _quiet_retries(monkeypatch):
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "0")
    monkeypatch.delenv("SONDER_HOSTED_OVERFLOW_RETRY", raising=False)


def _recording_post(monkeypatch, responder):
    seen = []

    def fake_post(path, payload, timeout=None):
        seen.append({
            "messages": list(payload.get("messages") or []),
            "options": dict(payload.get("options") or {}),
            "timeout": timeout,
        })
        return responder(len(seen))

    monkeypatch.setattr(server, "_post", fake_post)
    return seen


def test_local_overflow_compacts_and_retries_exactly_once(monkeypatch):
    def responder(call):
        if call == 1:
            raise _http_error(400, b'{"error":"context length exceeded"}')
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    _, content = server._chat_request(_payload(), model="local", timeout=30)

    assert content == "recovered"
    assert len(seen) == 2
    assert len(seen[1]["messages"]) < len(seen[0]["messages"])
    assert seen[1]["messages"][-1] == {"role": "user", "content": "live request"}


def test_compaction_retry_never_raises_num_ctx(monkeypatch):
    def responder(call):
        if call == 1:
            raise _http_error(400, b'{"error":"context length exceeded"}')
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    server._chat_request(_payload(num_ctx=8192), model="local", timeout=30)

    assert seen[0]["options"] == seen[1]["options"]
    assert seen[1]["options"]["num_ctx"] == 8192


def test_persistent_overflow_stops_after_one_compaction(monkeypatch):
    def responder(call):
        raise _http_error(400, b'{"error":"context length exceeded"}')

    seen = _recording_post(monkeypatch, responder)

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request(_payload(6), model="local", timeout=30)

    assert len(seen) == 2
    assert caught.value.status == 400


def test_compaction_retry_shares_the_original_timeout_budget(monkeypatch):
    def responder(call):
        if call == 1:
            raise _http_error(400, b'{"error":"context length exceeded"}')
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    server._chat_request(_payload(), model="local", timeout=17)

    assert seen[0]["timeout"] == 17
    assert 1 <= seen[1]["timeout"] <= 17


def test_misleading_rate_limit_status_still_compacts_locally(monkeypatch):
    def responder(call):
        if call == 1:
            raise _http_error(
                429,
                b'{"error":"maximum context length is 8192 tokens, however you requested 9000"}',
            )
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    _, content = server._chat_request(_payload(), model="local", timeout=30)

    assert content == "recovered"
    assert len(seen[1]["messages"]) < len(seen[0]["messages"])


def test_compaction_activity_does_not_disclose_provider_error_text(monkeypatch):
    secret = "tenant-secret-123"
    events = []

    def responder(call):
        if call == 1:
            raise _http_error(
                400,
                ('{"error":"requested 9000 tokens ' + secret
                 + ' but model context is only 8192"}').encode(),
            )
        return {"message": {"content": "recovered"}}

    monkeypatch.setattr(
        server.activity_tracker, "record_event",
        lambda event, **fields: events.append((event, fields)),
    )
    _recording_post(monkeypatch, responder)

    server._chat_request(_payload(), model="local", timeout=30)

    assert events and events[-1][0] == "model_context_compaction"
    assert "evidence" not in events[-1][1]
    assert secret not in repr(events)


def test_in_band_overflow_error_on_a_200_also_compacts(monkeypatch):
    def responder(call):
        if call == 1:
            return {"error": "context length exceeded"}
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    _, content = server._chat_request(_payload(), model="local", timeout=30)

    assert content == "recovered"
    assert len(seen) == 2


def test_error_redaction_preserves_literal_token_limit_overflow(monkeypatch):
    assert server._safe_model_error_detail("token limit exceeded") == "token limit exceeded"

    def responder(call):
        if call == 1:
            raise _http_error(400, b'{"error":"token limit exceeded"}')
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    _, content = server._chat_request(_payload(), model="local", timeout=30)

    assert content == "recovered"
    assert len(seen) == 2


def test_error_redaction_preserves_numeric_token_context_overflow(monkeypatch):
    detail = "12000 tokens exceeds the 8192 token context"
    assert server._safe_model_error_detail(detail) == detail

    def responder(call):
        if call == 1:
            raise _http_error(
                400,
                b'{"error":"12000 tokens exceeds the 8192 token context"}',
            )
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    _, content = server._chat_request(_payload(), model="local", timeout=30)

    assert content == "recovered"
    assert len(seen) == 2
    assert len(seen[1]["messages"]) < len(seen[0]["messages"])


def test_error_redaction_still_scrubs_credential_shaped_values():
    assert "super-secret-value" not in server._safe_model_error_detail(
        "token=super-secret-value",
    )
    assert "abc123" not in server._safe_model_error_detail("Bearer abc123")
    mixed = server._safe_model_error_detail(
        "12000 tokens exceeds the 8192 token context; token=super-secret-value",
    )
    assert "8192 token context" in mixed
    assert "super-secret-value" not in mixed


def test_in_band_non_overflow_error_is_still_reported(monkeypatch):
    seen = _recording_post(monkeypatch, lambda call: {"error": "model not found"})

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request(_payload(), model="local", timeout=30)

    assert caught.value.kind == "request"
    assert len(seen) == 1


def test_uncompactable_overflow_is_not_retried(monkeypatch):
    single = {
        "model": "local",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "x" * 4000},
        ],
        "options": {"num_ctx": 4096},
    }
    seen = _recording_post(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(
            _http_error(400, b'{"error":"context length exceeded"}')
        ),
    )

    with pytest.raises(server.ModelCallError):
        server._chat_request(single, model="local", timeout=30)

    assert len(seen) == 1


@pytest.mark.parametrize("status,body", [
    (413, b'{"error":"request entity too large"}'),
    (500, b'{"error":"CUDA out of memory"}'),
    (429, b'{"error":"rate limit reached, retry later"}'),
    (404, b'{"error":"model \'coder\' not found, try pulling it first"}'),
])
def test_non_overflow_failures_are_never_compaction_retried(monkeypatch, status, body):
    seen = _recording_post(
        monkeypatch, lambda call: (_ for _ in ()).throw(_http_error(status, body)),
    )

    with pytest.raises(server.ModelCallError):
        server._chat_request(_payload(), model="local", timeout=30)

    assert len(seen) == 1


def test_cancellation_suppresses_the_compaction_retry(monkeypatch):
    cancelled = {"value": False}

    def responder(call):
        if call == 1:
            cancelled["value"] = True
            raise _http_error(400, b'{"error":"context length exceeded"}')
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    with pytest.raises(server.ModelCallError):
        server._chat_request(
            _payload(), model="local", timeout=30,
            cancel_check=lambda: cancelled["value"],
        )

    assert len(seen) == 1


# --- gateway: hosted and remote routes stay off by default ------------------


def test_hosted_overflow_is_not_retried_by_default(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    seen = _recording_post(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(
            _http_error(400, b'{"error":"context length exceeded"}')
        ),
    )

    with pytest.raises(server.ModelCallError):
        server._chat_request(_payload(), model="hosted", cloud=True, timeout=30)

    assert len(seen) == 1


def test_hosted_idempotent_request_still_needs_the_operator_opt_in(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    seen = _recording_post(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(
            _http_error(400, b'{"error":"context length exceeded"}')
        ),
    )

    with pytest.raises(server.ModelCallError):
        server._chat_request(
            _payload(), model="hosted", cloud=True, timeout=30, idempotent=True,
        )

    assert len(seen) == 1


def test_hosted_opt_in_alone_does_not_retry_a_non_idempotent_request(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setenv("SONDER_HOSTED_OVERFLOW_RETRY", "1")
    seen = _recording_post(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(
            _http_error(400, b'{"error":"context length exceeded"}')
        ),
    )

    with pytest.raises(server.ModelCallError):
        server._chat_request(_payload(), model="hosted", cloud=True, timeout=30)

    assert len(seen) == 1


def test_hosted_retries_once_only_with_both_idempotence_and_opt_in(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setenv("SONDER_HOSTED_OVERFLOW_RETRY", "1")

    def responder(call):
        if call == 1:
            raise _http_error(400, b'{"error":"context length exceeded"}')
        return {"message": {"content": "recovered"}}

    seen = _recording_post(monkeypatch, responder)

    _, content = server._chat_request(
        _payload(), model="hosted", cloud=True, timeout=30, idempotent=True,
    )

    assert content == "recovered"
    assert len(seen) == 2
    assert len(seen[1]["messages"]) < len(seen[0]["messages"])


def test_cloud_wrapper_declares_inference_idempotent(monkeypatch):
    seen = []

    def fake_chat(payload, **kwargs):
        seen.append(kwargs)
        return {"message": {"content": "ok"}}, "ok"

    monkeypatch.setattr(server, "_chat_request", fake_chat)

    _, content, used_model = server._chat_request_with_cloud_fallback(
        _payload(), model="hosted", timeout=30,
    )

    assert content == "ok"
    assert used_model == "hosted"
    assert seen[0]["idempotent"] is True


def test_generate_wrapper_declares_remote_inference_idempotent(monkeypatch):
    seen = []

    def fake_chat(payload, **kwargs):
        seen.append(kwargs)
        return {"message": {"content": "ok"}}, "ok"

    monkeypatch.setattr(server, "_chat_request", fake_chat)
    generate = server._make_generate(
        "remote-local", "", 0.2, 128, 4096, cloud=False, timeout=30,
    )

    assert generate("hello") == "ok"
    assert seen[0]["idempotent"] is True


def test_remote_ollama_overflow_is_not_retried_by_default(monkeypatch):
    monkeypatch.setattr(server, "BASE", "http://models.example.test:11434")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    seen = _recording_post(
        monkeypatch,
        lambda call: (_ for _ in ()).throw(
            _http_error(400, b'{"error":"context length exceeded"}')
        ),
    )

    with pytest.raises(server.ModelCallError):
        server._chat_request(_payload(), model="remote-local", timeout=30)

    assert len(seen) == 1
