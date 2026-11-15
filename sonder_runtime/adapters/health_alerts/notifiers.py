"""Notifier adapters: Windows toast, ntfy-style HTTP push, log-only fallback, fan-out.

Every adapter returns a :class:`DeliveryResult` instead of raising, and every
``error`` is a short class-level reason. The ntfy URL is never put in a log
line, an exception message or a result.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence

from sonder_runtime.adapters.health_alerts._child import minimal_environment, system32_path
from sonder_runtime.application.ports.health_alerts import DeliveryResult, Notifier
from sonder_runtime.domain.health_alerts import Alert

logger = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Windows PowerShell 5.1 hosts the WinRT toast API; the alert text arrives in
# environment variables so no alert text is ever parsed as script. The id below
# is PowerShell's own AppUserModelID, which Windows already lets raise toasts.
_TOAST_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
    "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
    "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$t=$x.GetElementsByTagName('text');"
    "$t.Item(0).AppendChild($x.CreateTextNode($env:SONDER_ALERT_TITLE))|Out-Null;"
    "$t.Item(1).AppendChild($x.CreateTextNode($env:SONDER_ALERT_DETAIL))|Out-Null;"
    "$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
    "$id='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show($n)"
)

ProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]
HttpPost = Callable[[str, bytes, Mapping[str, str], float], int]


def _plain(text: str, limit: int) -> str:
    return _CONTROL.sub(" ", text)[:limit]


class ToastNotifier:
    channel = "toast"

    def __init__(
        self, *, timeout_seconds: float, run: ProcessRunner = subprocess.run,
        platform: str = sys.platform,
    ) -> None:
        self._timeout = timeout_seconds
        self._run = run
        self._platform = platform

    def send(self, alert: Alert) -> tuple[DeliveryResult, ...]:
        if not self._platform.startswith("win"):
            return (DeliveryResult(self.channel, False, "unsupported platform"),)
        powershell = system32_path("WindowsPowerShell", "v1.0", "powershell.exe")
        if not powershell:
            return (DeliveryResult(self.channel, False, "SystemRoot unknown"),)
        env = minimal_environment({
            "SONDER_ALERT_TITLE": _plain(alert.title, 120),
            "SONDER_ALERT_DETAIL": _plain(alert.detail, 240),
        })
        argv = [powershell, "-NoProfile", "-NonInteractive",
                "-WindowStyle", "Hidden", "-Command", _TOAST_SCRIPT]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            done = self._run(
                argv, env=env, capture_output=True, text=True,
                timeout=self._timeout, creationflags=flags, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return (DeliveryResult(self.channel, False, type(exc).__name__),)
        if done.returncode != 0:
            return (DeliveryResult(self.channel, False, "powershell exit %d" % done.returncode),)
        return (DeliveryResult(self.channel, True),)


def _urllib_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme checked by config
        return int(response.status)


class NtfyNotifier:
    """POST the alert text to an ntfy-style topic URL taken from the secrets."""

    channel = "ntfy"

    def __init__(
        self, url: str, *, timeout_seconds: float, post: HttpPost = _urllib_post,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._post = post

    def __repr__(self) -> str:
        return "NtfyNotifier(url=<redacted>)"

    def send(self, alert: Alert) -> tuple[DeliveryResult, ...]:
        title = _plain(alert.title, 120).encode("ascii", "replace").decode("ascii")
        headers = {"Title": title, "Priority": "high", "Tags": "warning",
                   "Content-Type": "text/plain; charset=utf-8"}
        try:
            status = self._post(self._url, alert.detail.encode("utf-8"), headers, self._timeout)
        except urllib.error.HTTPError as exc:
            return (DeliveryResult(self.channel, False, "http %d" % exc.code),)
        except Exception as exc:  # noqa: BLE001 - class name only: messages can embed the URL
            return (DeliveryResult(self.channel, False, type(exc).__name__),)
        if not 200 <= status < 300:
            return (DeliveryResult(self.channel, False, "http %d" % status),)
        return (DeliveryResult(self.channel, True),)


class LogNotifier:
    """Always-available fallback: the alert goes to the error log."""

    channel = "log"

    def send(self, alert: Alert) -> tuple[DeliveryResult, ...]:
        logger.error("HEALTH ALERT [%s] %s: %s", alert.key, alert.title, alert.detail)
        return (DeliveryResult(self.channel, True),)


class FanoutNotifier:
    """Send through every configured channel; fall back to the log when none delivered."""

    def __init__(self, channels: Sequence[Notifier], fallback: Notifier | None = None) -> None:
        self._channels = tuple(channels)
        self._fallback = fallback if fallback is not None else LogNotifier()

    def send(self, alert: Alert) -> tuple[DeliveryResult, ...]:
        results: list[DeliveryResult] = []
        for channel in self._channels:
            results.extend(channel.send(alert))
        if not any(r.delivered for r in results):
            results.extend(self._fallback.send(alert))
        return tuple(results)


__all__ = ["FanoutNotifier", "LogNotifier", "NtfyNotifier", "ToastNotifier"]
