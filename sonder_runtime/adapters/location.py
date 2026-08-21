"""Canonical consent-gated approximate-location adapter."""
from __future__ import annotations

import importlib
import re

from ..application.context import OperationContext


def _web_tools():
    return importlib.import_module("web_tools")


IP_LOCATION_URL = ("https://ipwho.is/?fields=success,message,country,country_code,"
                   "region,region_code,city,timezone")
IP_LOCATION_DOCS_URL = "https://ipwhois.io/documentation"


def _json_request(url, timeout=10):
    return _web_tools()._json_request(url, timeout=timeout)


def normalize_location_hint(data):
    if not isinstance(data, dict):
        raise ValueError("location hint must be an object")
    if data.get("success") is False:
        raise ValueError(str(data.get("message") or "IP location lookup failed"))
    result = {}
    for key in ("city", "region", "region_code", "country", "country_code", "timezone"):
        raw_value = data.get(key)
        if key == "timezone" and isinstance(raw_value, dict):
            raw_value = raw_value.get("id") or raw_value.get("name")
        if isinstance(raw_value, (dict, list, tuple, set)):
            raise ValueError("location hint contains an invalid %s" % key)
        value = re.sub(r"\s+", " ", str(raw_value or "")).strip()
        if value:
            if len(value) > 120 or any(ord(char) < 32 for char in value):
                raise ValueError("location hint contains an invalid %s" % key)
            result[key] = value
    if not (result.get("city") or result.get("region") or result.get("country")):
        raise ValueError("location lookup did not return a place")
    result.update({"approximate": True, "source": "ipwho.is", "source_url": IP_LOCATION_DOCS_URL})
    return result


def approximate_location_lookup(timeout=10):
    if not _web_tools().enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    return normalize_location_hint(_json_request(IP_LOCATION_URL, timeout=timeout))


def location_label(location):
    location = normalize_location_hint(location)
    parts = [location.get("city"), location.get("region"), location.get("country")]
    return ", ".join(str(part) for i, part in enumerate(parts) if part and part not in parts[:i])


def format_approximate_location(location):
    location = normalize_location_hint(location)
    lines = ["Approximate location: %s" % location_label(location)]
    if location.get("timezone"):
        lines.append("Timezone: %s" % location["timezone"])
    lines.extend(["Accuracy: city/region estimate from the public IP; VPNs and ISP routing can make it wrong.", "Raw IP: not retained or displayed.", "Source: ipwho.is (%s)" % location.get("source_url", IP_LOCATION_DOCS_URL)])
    return "\n".join(lines)


def lookup(*, consent=False, context: OperationContext):
    if not consent or not context.cloud_allowed:
        raise PermissionError("explicit location and cloud consent are required")
    location = approximate_location_lookup()
    return {
        "ok": True,
        "label": location_label(location),
        "text": format_approximate_location(location),
    }


def format_result(result):
    return str(result.get("text", ""))


__all__ = ["approximate_location_lookup", "format_approximate_location", "format_result", "location_label", "lookup", "normalize_location_hint"]
