"""Operator settings for gated desktop computer use. Off unless configured.

Enabling the section grants nothing by itself: a driving session must still be
approved at the Sonder console (``computer_use_start`` is graded
``dangerous``), every action passes the permission gate, and only windows of
the executables listed in ``allowed_apps`` can be seen or driven.
"""

from dataclasses import dataclass
import re

# Executables whose Enter key sends a message; Enter there needs confirmation.
DEFAULT_SUBMIT_ON_ENTER_APPS = (
    "outlook.exe", "olk.exe", "thunderbird.exe", "slack.exe", "discord.exe",
    "teams.exe", "ms-teams.exe", "whatsapp.exe", "telegram.exe", "signal.exe",
)

_APP_NAME = re.compile(r"[a-z0-9][a-z0-9 ._+-]{0,126}\.exe")


@dataclass(frozen=True)
class ComputerUseConfig:
    enabled: bool = False
    # Lower-case executable file names ("notepad.exe"); no paths, no wildcards.
    allowed_apps: tuple[str, ...] = ()
    submit_on_enter_apps: tuple[str, ...] = DEFAULT_SUBMIT_ON_ENTER_APPS
    session_ttl_seconds: int = 900
    max_actions_per_minute: int = 60
    max_actions_per_session: int = 500
    max_task_steps: int = 25
    # Ask the vision model what control sits under a click, and confirm the
    # click when either the caller's label or the model's reading of the
    # screen looks irreversible. It can only add confirmations, never remove one.
    verify_clicks: bool = True


def computer_use_errors(config):
    c = config.computer_use
    errors = []
    for key in ("enabled", "verify_clicks"):
        if type(getattr(c, key)) is not bool:
            errors.append(f"[computer_use].{key} must be boolean")
    for key in ("allowed_apps", "submit_on_enter_apps"):
        values = getattr(c, key)
        if (
            type(values) is not tuple
            or len(values) > 64
            or len(set(values)) != len(values)
            or any(type(v) is not str or not _APP_NAME.fullmatch(v) for v in values)
        ):
            errors.append(
                f"[computer_use].{key} must be unique lower-case executable names "
                "such as \"notepad.exe\""
            )
    for key, low, high in (
        ("session_ttl_seconds", 60, 4 * 3600),
        ("max_actions_per_minute", 1, 600),
        ("max_actions_per_session", 1, 10000),
        ("max_task_steps", 1, 200),
    ):
        value = getattr(c, key)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"[computer_use].{key} must be an integer in [{low}, {high}]")
    if c.enabled is True and not c.allowed_apps:
        errors.append("[computer_use] enabled with no allowed_apps drives nothing")
    return errors
