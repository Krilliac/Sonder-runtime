"""Unit tests for the secret guard: encodings, allow list, fail-closed set-up, model metadata, no leaks of its own."""
from __future__ import annotations

import base64
import json
import struct
from urllib.parse import quote, quote_plus

import pytest

from sonder_runtime.adapters.meshgen import secret_sources
from sonder_runtime.domain.meshgen.secret_guard import REDACTED, SecretGuard, SecretGuardUnavailable

VALUE = "canary-9f8e7d6c5b4a39281706"  # 27 chars, lowercase hex tail: matches no key shape by itself
NAME = "SONDER_CANARY_TOKEN"


def guard(**kwargs):
    return SecretGuard({NAME: VALUE}, **kwargs)


def glb(document: dict) -> bytes:
    body = json.dumps(document).encode("utf-8")
    body += b" " * (-len(body) % 4)
    return b"glTF" + struct.pack("<II", 2, 20 + len(body)) + struct.pack("<II", len(body), 0x4E4F534A) + body


def test_the_plain_value_matches_and_unrelated_text_does_not():
    g = guard()
    assert g.hit("boom: " + VALUE + " end")
    assert not g.hit("ValueError: the cube has 12 faces")
    assert g.redact("x " + VALUE) == (REDACTED, True)
    assert g.redact("fine") == ("fine", False)


@pytest.mark.parametrize("encode", [
    lambda v: v.upper(),
    lambda v: v[::-1],
    lambda v: quote(v, safe=""),
    lambda v: v.encode().hex(),
    lambda v: v.encode().hex().upper(),
    lambda v: base64.b64encode(v.encode()).decode(),
    lambda v: base64.urlsafe_b64encode(v.encode()).decode().rstrip("="),
    lambda v: base64.b64encode(b"x" + v.encode()).decode(),
    lambda v: base64.b64encode(b"xy" + v.encode()).decode(),
    lambda v: base64.b64encode(b"xyz" + v.encode() + b"!").decode(),
])
def test_every_encoded_form_of_a_secret_is_caught(encode):
    text = "name=" + encode(VALUE) + ";"
    assert guard().hit(text), text


def test_url_encoded_forms_are_caught_for_a_value_with_reserved_characters():
    value = "p@ss word/with+reserved&chars=1234"
    g = SecretGuard({"MY_PASSWORD": value})
    for form in (value, quote(value, safe=""), quote_plus(value), value.lower(), value[::-1],
                 base64.b64encode(value.encode()).decode(), value.encode().hex()):
        assert g.hit("x" + form + "y"), form


def test_all_three_base64_alignments_are_caught_inside_larger_payloads():
    g = guard()
    for prefix in (b"", b"a", b"ab"):
        blob = base64.b64encode(b"HEADER" + prefix + VALUE.encode() + b"TRAILER").decode()
        assert g.hit(blob), prefix


def test_names_decide_what_is_a_secret_and_short_values_are_ignored():
    g = SecretGuard({"MY_API_KEY": "value-one-123456", "PATH": "C:\\Windows;C:\\Windows\\System32",
                     "SHORT_TOKEN": "tiny", "COLOR": "plain-colour-value-xyz"})
    assert g.hit("value-one-123456")
    assert not g.hit("C:\\Windows;C:\\Windows\\System32")  # a non-credential name with an ordinary value
    assert not g.hit("tiny")
    assert not g.hit("plain-colour-value-xyz")
    assert g.secret_count == 1


def test_a_credential_shaped_value_counts_even_under_an_innocent_name():
    g = SecretGuard({"HARMLESS": "ghp_" + "A1b2C3d4E5f6G7h8I9j0"})
    assert g.hit("leak ghp_A1b2C3d4E5f6G7h8I9j0 here")


def test_the_shell_working_directory_is_not_a_secret():
    g = SecretGuard({"PWD": "/home/user/projects/something", "OLDPWD": "/home/user/projects/other"})
    assert g.secret_count == 0 and not g.hit("/home/user/projects/something")


@pytest.mark.parametrize("text", [
    "sk-ant-api03-AbCdEf123456", "key sk-AbCdEfGhIjKlMnOpQr", "ghp_" + "a" * 20, "gho_" + "b" * 20,
    "github_pat_" + "c" * 20, "AKIAABCDEFGHIJKLMNOP", "xoxb-1234567890-abcdef", "xoxs-1234567890-abcdef",
    "AIza" + "d" * 35, "-----BEGIN PRIVATE KEY-----", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123",
    "hf_" + "e" * 25, "Zk3Qw9Xr7TnB2LvC8MpD4YsH6JgF1aKe5UoR",
])
def test_key_shapes_are_caught_even_when_the_value_is_not_in_the_set(text):
    assert SecretGuard({}).hit("prefix " + text + " suffix")


@pytest.mark.parametrize("text", [
    "result-0123456789abcdef0123456789abcdef.json", "a-rather-long-hyphenated-object-name-for-a-wheelbarrow-handle",
    "Cube.001", "C:\\Users\\someone\\out\\c0_r1\\wheelbarrow.glb", "task-list item", "0123456789abcdef0123456789abcdef",
    "the quick brown fox jumps over the lazy dog many many times",
    "    bpy.utils.execfile(r'C:/Users/Nathan/AppData/Local/Temp/mgdbg-w_y1_uvl/payload.py')",
])
def test_ordinary_text_is_not_flagged(text):
    assert not SecretGuard({}).hit(text)


def test_the_allow_list_exempts_exactly_the_listed_names():
    entries = {"SONDER_CANARY_TOKEN": VALUE, "OTHER_API_KEY": "another-secret-value-12345"}
    open_guard = SecretGuard(entries, allow=["sonder_canary_token"])
    assert not open_guard.hit(VALUE)  # listed (case-insensitive) -> exempt
    assert open_guard.hit("another-secret-value-12345")  # not listed -> still guarded
    assert SecretGuard(entries).hit(VALUE)  # default: nothing is exempt
    shaped = SecretGuard({"MY_KEY": "ghp_A1b2C3d4E5f6G7h8I9j0"}, allow=["MY_KEY"])
    assert not shaped.hit("ghp_A1b2C3d4E5f6G7h8I9j0")  # a shape that is part of an allowed value passes
    assert shaped.hit("ghp_ZZZZZZZZZZZZZZZZZZZZ")  # another credential-shaped string does not


def test_allowed_names_are_read_from_the_environment_variable():
    assert secret_sources.allowed_names({}) == []
    assert secret_sources.allowed_names({"SONDER_MESHGEN_SECRET_ALLOW": " A_KEY , B_TOKEN ,,"}) == ["A_KEY", "B_TOKEN"]
    built = secret_sources.load_guard({"A_KEY": VALUE, "SONDER_MESHGEN_SECRET_ALLOW": "A_KEY"})
    assert not built.hit(VALUE)
    assert secret_sources.load_guard({"A_KEY": VALUE}).hit(VALUE)


def test_load_guard_reads_the_registry_environment_and_fails_closed(monkeypatch):
    monkeypatch.setattr(secret_sources, "read_hkcu_environment", lambda: [("REG_SECRET", VALUE)])
    assert secret_sources.load_guard({}).hit(VALUE)

    def broken():
        raise SecretGuardUnavailable("cannot read the user environment from the registry (PermissionError)")

    monkeypatch.setattr(secret_sources, "read_hkcu_environment", broken)
    with pytest.raises(SecretGuardUnavailable):
        secret_sources.load_guard({})


def test_a_registry_that_cannot_be_opened_refuses_instead_of_returning_an_empty_set(monkeypatch):
    import os
    if os.name != "nt":
        pytest.skip("winreg exists only on Windows")
    import winreg

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(winreg, "OpenKey", denied)
    with pytest.raises(SecretGuardUnavailable) as caught:
        secret_sources.read_hkcu_environment()
    assert "PermissionError" in str(caught.value)


def test_an_unexpected_failure_while_building_the_guard_refuses(monkeypatch):
    monkeypatch.setattr(secret_sources, "read_hkcu_environment", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(SecretGuardUnavailable):
        secret_sources.load_guard({})


def test_glb_json_chunk_names_extras_and_asset_are_scanned():
    g = guard()
    clean = glb({"asset": {"version": "2.0", "generator": "Blender"}, "nodes": [{"name": "Cube"}]})
    assert not g.glb_hit(clean)
    for document in ({"nodes": [{"name": VALUE}]}, {"asset": {"extras": {"note": VALUE}}},
                     {"materials": [{"name": base64.b64encode(VALUE.encode()).decode()}]},
                     {"meshes": [{"name": "m", "extras": {"k": [VALUE[::-1]]}}]}):
        assert g.glb_hit(glb(document)), document


def test_a_malformed_glb_is_treated_as_a_leak():
    g = guard()
    assert g.glb_hit(b"glTF")
    assert g.glb_hit(b"not a glb at all, just some bytes padding")
    broken = bytearray(glb({"a": 1}))
    broken[20] = 0xFF
    assert g.glb_hit(bytes(broken))


def test_model_files_are_scanned_and_unreadable_files_fail_closed(tmp_path):
    g = guard()
    good, bad = tmp_path / "a.glb", tmp_path / "b.glb"
    good.write_bytes(glb({"nodes": [{"name": "Cube"}]}))
    bad.write_bytes(glb({"nodes": [{"name": VALUE}]}))
    fbx = tmp_path / "c.fbx"
    fbx.write_bytes(b"Kaydara FBX Binary\x00\x1a\x00" + b"\x00" * 40 + b"Model::" + VALUE.encode() + b"\x00" * 20)
    clean_fbx = tmp_path / "d.fbx"
    clean_fbx.write_bytes(b"Kaydara FBX Binary\x00\x1a\x00" + bytes(range(256)) * 8 + b"Model::Cube\x00")
    assert not g.model_hit(good) and g.model_hit(bad) and g.model_hit(fbx) and not g.model_hit(clean_fbx)
    assert g.model_hit(tmp_path / "missing.glb")


def test_vertex_data_is_not_scanned():
    g = guard()
    document = {"nodes": [{"name": "Cube"}]}
    body = json.dumps(document).encode("utf-8")
    body += b" " * (-len(body) % 4)
    binary = VALUE.encode().ljust(32, b"\0")
    data = (b"glTF" + struct.pack("<II", 2, 20 + len(body) + 8 + len(binary)) + struct.pack("<II", len(body), 0x4E4F534A)
            + body + struct.pack("<II", len(binary), 0x004E4942) + binary)
    assert not g.glb_hit(data)  # documented residual risk: secrets in vertex data stay local


def test_scrub_redacts_strings_keys_and_nested_values_and_counts_hits():
    g = guard()
    payload = {"error": "ValueError: " + VALUE, "ok": False, "n": 3, "tail": ["fine", VALUE, ["deep", VALUE]],
               VALUE: "key was the secret", "files": {"model": "C:/out/x.glb"}}
    clean, hits = g.scrub(payload)
    assert hits == 4
    dumped = json.dumps(clean)
    assert VALUE not in dumped and dumped.count(REDACTED) >= 4
    assert clean["n"] == 3 and clean["files"] == {"model": "C:/out/x.glb"} and clean["tail"][0] == "fine"
    assert g.scrub({"a": ["b", 1, None]})[1] == 0


def test_nothing_the_guard_emits_or_raises_contains_a_name_or_value(tmp_path):
    g = SecretGuard({"MY_SPECIAL_API_KEY": VALUE})
    outputs = [repr(g), str(g.redact("x" + VALUE)), json.dumps(g.scrub({"e": VALUE})[0]), g.secret_count]
    for item in outputs:
        assert VALUE not in str(item) and "MY_SPECIAL_API_KEY" not in str(item)
    unreadable = tmp_path / "missing.glb"
    assert g.model_hit(unreadable) is True  # fail closed with no message to leak from
    try:
        raise SecretGuardUnavailable("cannot build the secret guard (OSError)")
    except SecretGuardUnavailable as exc:
        assert VALUE not in str(exc) and "MY_SPECIAL_API_KEY" not in str(exc)


def test_json_unicode_escapes_in_the_glb_chunk_do_not_hide_a_secret():
    escaped = "".join(chr(92) + "u%04x" % ord(c) for c in VALUE)
    body = ('{"nodes":[{"name":"%s"}]}' % escaped).encode("ascii")
    body += b" " * (-len(body) % 4)
    data = b"glTF" + struct.pack("<II", 2, 20 + len(body)) + struct.pack("<II", len(body), 0x4E4F534A) + body
    assert VALUE.encode() not in data and guard().glb_hit(data)
