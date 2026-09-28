"""Pure rules for gated computer use (sonder_runtime/domain/computer_use/rules.py)."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.desktop import png
from sonder_runtime.domain.computer_use import rules
from sonder_runtime.platform.computer_use_config import ComputerUseConfig, computer_use_errors


@pytest.mark.parametrize("keys, chord", [
    ("ctrl+s", ("ctrl", "s")),
    ("Shift+Ctrl+Z", ("ctrl", "shift", "z")),
    ("return", ("enter",)),
    ("alt+f4", ("alt", "f4")),
])
def test_chords_are_canonical(keys, chord):
    assert rules.parse_chord(keys) == chord


@pytest.mark.parametrize("keys", [
    "win+r", "meta+l", "alt+tab", "shift+alt+tab", "ctrl+escape", "ctrl+shift+esc",
    "ctrl+alt+delete", "alt+space", "ctrl+ctrl+s", "ctrl+", "hyper+x", "",
])
def test_chords_that_leave_the_window_or_are_malformed_are_refused(keys):
    with pytest.raises(rules.ActionRefused):
        rules.parse_chord(keys)


@pytest.mark.parametrize("label", [
    "Send", "Delete account", "Place order", "Pay now", "Submit", "Publish post",
    "Empty Recycle Bin", "Uninstall", "unverified control (treat as submit)",
])
def test_irreversible_click_labels_need_confirmation(label):
    assert rules.irreversible_reason("click", labels=["", label])


@pytest.mark.parametrize("label", ["Sender details", "Edit", "Settings", "Postcode", "Ordering tips"])
def test_ordinary_click_labels_do_not(label):
    assert rules.irreversible_reason("click", labels=[label]) == ""


def test_the_screen_can_add_a_confirmation_the_caller_did_not_name():
    # The caller says "Next"; the vision model reads the control as "Delete forever".
    assert rules.irreversible_reason("click", labels=["Next", "Delete forever"])


def test_keys_and_text_that_send_or_destroy():
    submit = ("slack.exe",)
    assert rules.irreversible_reason("key", chord=("enter",), app="Slack.exe", submit_on_enter_apps=submit)
    assert rules.irreversible_reason("key", chord=("enter",), app="notepad.exe", submit_on_enter_apps=submit) == ""
    assert rules.irreversible_reason("type", text="hi\n", app="slack.exe", submit_on_enter_apps=submit)
    assert rules.irreversible_reason("type", text="hi\n", app="notepad.exe", submit_on_enter_apps=submit) == ""
    assert rules.irreversible_reason("key", chord=("shift", "delete"), app="notepad.exe")
    assert rules.irreversible_reason("key", chord=("delete",), app="explorer.exe")
    assert rules.irreversible_reason("key", chord=("alt", "f4"), app="notepad.exe")


def test_normalized_coordinates_match_the_measured_qwen_grid():
    # Measured 2026-09-28: Qwen3.8 answered (812, 873) for a button centred at
    # (1040, 700) in a 1280x800 screenshot -- a 0..1000 grid, not pixels.
    assert rules.to_pixels(812, 873, "normalized", 1280, 800) == (1039, 698)
    assert rules.to_pixels(1000, 1000, "normalized", 1280, 800) == (1279, 799)
    with pytest.raises(rules.ActionRefused):
        rules.to_pixels(1001, 5, "normalized", 1280, 800)
    with pytest.raises(rules.ActionRefused):
        rules.to_pixels(-1, 5, "pixels", 1280, 800)


def test_build_action_contract():
    act = rules.build_action("click", x=500, y=500, coords="normalized", width=200, height=100,
                             label="  OK\x07 ")
    assert (act.x, act.y, act.label) == (100, 50, "OK")
    with pytest.raises(rules.ActionRefused, match="target_label"):
        rules.build_action("click", x=1, y=1, coords="pixels", width=10, height=10)
    with pytest.raises(rules.ActionRefused):
        rules.build_action("type", text="")
    with pytest.raises(rules.ActionRefused):
        rules.build_action("type", text="a\x1bb")
    with pytest.raises(rules.ActionRefused):
        rules.build_action("scroll", x=1, y=1, coords="pixels", width=10, height=10, scroll=0)
    with pytest.raises(rules.ActionRefused):
        rules.build_action("launch", x=1, y=1)
    assert rules.build_action("type", text="a\r\nb").text == "a\nb"


def test_action_budget_limits_rate_and_session_total():
    now = [0.0]
    budget = rules.ActionBudget(2, 3, clock=lambda: now[0])
    assert budget.admit() == "" and budget.admit() == ""
    assert "minute" in budget.admit()
    now[0] = 61.0
    assert budget.admit() == ""
    assert "budget" in budget.admit()


def test_model_step_is_parsed_to_a_closed_shape():
    step = rules.parse_model_step(
        'Sure! {"action": "Click", "x": 10, "y": 20, "label": "OK", "evil": 1, "done": "yes"} done')
    assert step["action"] == "click" and step["done"] is False and "evil" not in step
    assert rules.parse_model_step('{"done": true, "reason": "finished"}')["done"] is True
    with pytest.raises(rules.ActionRefused):
        rules.parse_model_step("no json here")


def test_png_roundtrip_and_crop():
    width, height = 3, 2
    bgra = bytes([0, 0, 255, 0, 0, 255, 0, 0, 255, 0, 0, 0,
                  10, 20, 30, 0, 40, 50, 60, 0, 70, 80, 90, 0])
    data = png.encode_bgra(width, height, bgra)
    w, h, rgb = png.decode_rgb(data)
    assert (w, h) == (3, 2)
    assert rgb[:3] == bytes([255, 0, 0]) and rgb[9:12] == bytes([30, 20, 10])
    cw, ch, crop = png.decode_rgb(png.crop_rgb_png(data, 1, 1, 3, 2))
    assert (cw, ch, crop) == (2, 1, bytes([60, 50, 40, 90, 80, 70]))


class _Config:
    def __init__(self, cu):
        self.computer_use = cu


def test_config_defaults_are_off_and_valid():
    assert computer_use_errors(_Config(ComputerUseConfig())) == []
    assert ComputerUseConfig().enabled is False


@pytest.mark.parametrize("cu", [
    ComputerUseConfig(enabled=True),
    ComputerUseConfig(allowed_apps=("C:/Windows/notepad.exe",)),
    ComputerUseConfig(allowed_apps=("*.exe",)),
    ComputerUseConfig(allowed_apps=("notepad.exe", "notepad.exe")),
    ComputerUseConfig(session_ttl_seconds=5),
    ComputerUseConfig(max_actions_per_minute=0),
])
def test_config_errors(cu):
    assert computer_use_errors(_Config(cu))
