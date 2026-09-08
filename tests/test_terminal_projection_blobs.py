from dataclasses import asdict, replace
import hashlib
import json

import pytest

from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec, _canonical
from sonder_runtime.application.ports.terminal_output import TerminalOutputReference
from tests.test_host_terminal_projection import make
from tests.test_terminal_output import data, store_for


class Store:
    """Port double; durability is verified by the separate persistence suite."""
    def __init__(self):
        self.rows = {}
        self.puts = 0
        self.corrupt = False

    def put(self, binding, output, *, context):
        assert context is TOKEN
        self.puts += 1
        key = hashlib.sha256(_canonical(asdict(binding))).hexdigest()
        self.rows[key] = output
        return TerminalOutputReference(hashlib.sha256(output.encode()).hexdigest(),
            len(output.encode()), key)

    def get(self, binding, reference, *, context):
        assert context is TOKEN
        key = hashlib.sha256(_canonical(asdict(binding))).hexdigest()
        assert key == reference.binding_sha256
        return 'corrupted' if self.corrupt else self.rows[key]


TOKEN = object()


def codec(store):
    return TerminalProjectionCodec(output_store=store, output_context=lambda binding: TOKEN)


def test_long_unicode_output_reopens_exactly_without_another_write(tmp_path):
    store = Store()
    first = codec(store)
    text = 'Exact \u2603 output\r\n' * 5000
    original = make(first, tmp_path, text)
    payload = first.encode(original)
    assert len(payload) < 4096
    assert text.encode() not in payload
    restored = codec(store).decode(payload)
    assert restored.output == text
    assert codec(store).decode(payload).output == text
    assert store.puts == 1


def test_blob_corruption_and_missing_store_refuse_restoration(tmp_path):
    store = Store()
    first = codec(store)
    payload = first.encode(make(first, tmp_path, 'x' * 20000))
    store.corrupt = True
    with pytest.raises(ValueError):
        codec(store).decode(payload)
    with pytest.raises(ValueError):
        TerminalProjectionCodec().decode(payload)


def test_wrong_binding_receipt_refused_before_projection_is_issued(tmp_path):
    class WrongStore(Store):
        def put(self, *args, **kwargs):
            return replace(super().put(*args, **kwargs), binding_sha256='0' * 64)
    with pytest.raises(ValueError):
        make(codec(WrongStore()), tmp_path, 'x' * 20000)


def test_oversized_blob_is_refused_before_storage_and_failed_output_stays_failed(tmp_path):
    store = Store()
    first = codec(store)
    with pytest.raises(ValueError):
        make(first, tmp_path, 'x' * (1024 * 1024 + 1))
    assert store.puts == 0
    result = first.decode(first.encode(make(first, tmp_path, 'CANCELLED\n' + 'x' * 20000)))
    assert first.parent_effects_valid(result) is False


def test_malformed_blob_reference_never_reaches_store(tmp_path):
    store = Store()
    first = codec(store)
    value = json.loads(first.encode(make(first, tmp_path, 'x' * 20000)))
    value['output_reference']['size_bytes'] = True
    with pytest.raises(ValueError):
        first.decode(_canonical(value))


def test_actual_private_store_and_new_codec_restore_after_reopen(data):
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger

    _, project, context, binding = data
    first = TerminalProjectionCodec(output_store=store_for(data),
        output_context=lambda binding: context)
    text = 'original output \u2603\r\n' * 6000
    original = first.capture(binding=binding,
        ledger=HostObservationLedger(project_scope=str(project)), output=text,
        terminal_class='NORMAL', blockers=(), terminal_receipt_id='receipt')
    payload = first.encode(original)
    reopened = TerminalProjectionCodec(output_store=store_for(data),
        output_context=lambda binding: context)
    restored = reopened.decode(payload)
    assert restored.output == text
    assert reopened.encode(restored) == payload
    denied = TerminalProjectionCodec(output_store=store_for(data),
        output_context=lambda binding: replace(context, principal_id='other'))
    with pytest.raises(PermissionError):
        denied.decode(payload)
