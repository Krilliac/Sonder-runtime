"""Host-I/O implementation of the startup preflight contract."""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass  # noqa: F401 - legacy star-import surface
from pathlib import Path
from urllib.parse import urlsplit

import sonder_runtime.adapters.persistence.migrations as sonder_migrations
from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.platform.config import SonderConfig

from ..application.ports.preflight import CheckResult, PreflightReport


def _check_state_directories(config: SonderConfig) -> list[CheckResult]:
    results = []
    home = Path(config.state.home).expanduser()
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".sonder-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        results.append(CheckResult("state_home_writable", True, True, str(home)))
    except OSError as exc:
        results.append(
            CheckResult("state_home_writable", False, True, f"{home}: {exc}")
        )
    for root in config.state.workspace_roots:
        path = Path(root).expanduser()
        ok = path.is_dir() and os.access(path, os.W_OK)
        results.append(
            CheckResult(
                f"workspace_root:{root}",
                ok,
                True,
                str(path) if ok else f"{path} is not a writable directory",
            )
        )
    return results


def _check_disk_space(config: SonderConfig) -> CheckResult:
    home = Path(config.state.home).expanduser()
    target = home if home.exists() else home.parent
    try:
        free = shutil.disk_usage(target).free
    except OSError as exc:
        return CheckResult("disk_space", False, True, f"{target}: {exc}")
    needed = config.state.minimum_free_disk_bytes
    return CheckResult(
        "disk_space", free >= needed, True, f"free={free} required={needed}"
    )


def _check_schema_versions(config: SonderConfig) -> list[CheckResult]:
    del config
    results = []
    try:
        statuses = sonder_migrations.status_all()
    except Exception as exc:
        return [CheckResult("schema_versions", False, True, str(exc))]
    for store, status in statuses.items():
        required = True
        if status.unknown:
            detail = f"future schema: unknown migrations {list(status.unknown)}"
            ok = False
        elif status.checksum_mismatches:
            detail = "migration history modified: %s" % list(
                status.checksum_mismatches
            )
            ok = False
        elif status.pending:
            detail = f"pending migrations: {list(status.pending)}"
            ok = False
            required = False
        else:
            detail = f"{len(status.applied)} applied"
            ok = True
        results.append(CheckResult(f"schema:{store}", ok, required, detail))
    return results


def _check_runtime_policy() -> CheckResult:
    try:
        import sonder_runtime.adapters.runtime_policy as runtime_policy

        policy = runtime_policy.load()
        return CheckResult(
            "runtime_policy",
            True,
            True,
            f"revision={policy.get('revision', 'unknown')}",
        )
    except Exception as exc:
        return CheckResult("runtime_policy", False, True, str(exc))


def _check_inference_origin(
    origin: str,
    *,
    name: str,
    required: bool,
    timeout: float,
    allow_remote: bool = False,
) -> CheckResult:
    url = origin.rstrip("/") + "/v1/sonder/health"
    host = urlsplit(origin).hostname or ""
    try:
        from sonder_runtime.adapters.inference.sonder_inference_gateway import (
            resolve_api_key,
        )
        headers: dict[str, str] = {}
        api_key = resolve_api_key()
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(url, headers=headers, method="GET")
        with ollama_endpoint.open_url(
            request, timeout=timeout, allow_remote=allow_remote is True,
        ) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("health response exceeded 1 MiB")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("health response must be an object")
            state = str(payload.get("status") or payload.get("state") or "").strip().lower()
            models = len(payload.get("models") or [])
            if response.status != 200 or state != "ready":
                return CheckResult(
                    name, False, required,
                    f"{host}: HTTP {response.status} state={state or 'unknown'}",
                )
            return CheckResult(name, True, required, f"{host}: ready ({models} models)")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return CheckResult(name, False, required, f"{host}: {exc}")


def _check_inference(config: SonderConfig, *, timeout: float = 5.0) -> CheckResult:
    if getattr(getattr(config, "membership", None), "mode", None) == "external":
        return CheckResult(
            "inference", False, False,
            "deferred: external membership requires explicit typed pool refresh",
        )
    inf = getattr(config, "inference", None)
    url = (
        getattr(inf, "url", "")
        or os.environ.get("SONDER_INFERENCE_BASE_URL", "").strip()
        or "http://127.0.0.1:11437"
    )
    allow_remote = bool(getattr(inf, "allow_remote", False))
    return _check_inference_origin(
        url,
        name="inference",
        required=True,
        timeout=timeout,
        allow_remote=allow_remote,
    )


_check_ollama_origin = _check_inference_origin
_check_ollama = _check_inference


def _check_ollama_workers(
    config: SonderConfig, *, timeout: float = 5.0,
) -> list[CheckResult]:
    """SI private workers are probed via SonderInferenceGateway; no legacy Ollama pool."""
    if getattr(getattr(config, "membership", None), "mode", None) == "external":
        return [CheckResult(
            "inference_workers", False, False,
            "deferred: external membership requires explicit typed pool refresh",
        )]
    return []


def _sonder_inference_result() -> CheckResult | None:
    from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env

    try:
        bindings = provider_bindings_from_env()
    except ValueError as exc:
        return CheckResult("sonder_inference", False, False, f"invalid provider bindings: {exc}")
    if "sonder_inference" not in bindings.bound_providers:
        return None
    from sonder_runtime.adapters.inference.sonder_inference_gateway import (
        SonderInferenceGateway,
    )

    readiness = SonderInferenceGateway().readiness()
    ready = readiness.kind == "ready"
    detail = readiness.detail
    if ready and readiness.synthetic:
        detail += " (MOCK backend: synthetic output)"
    fallback = bindings.fallbacks.get("sonder_inference")
    if readiness.kind == "unreachable" and fallback:
        detail += f"; requests it never receives fall back to {fallback}"
    elif readiness.kind == "misconfigured":
        detail += "; every request fails until this is fixed"
    return CheckResult("sonder_inference", ready, False, detail)


def _check_openrouter() -> CheckResult | None:
    """Configuration-only note for a bound OpenRouter provider.

    Never contacts OpenRouter (a paid API is never probed automatically) and
    never names the key's value: it reports opt-in, key presence and whether
    every bound tier resolves to a model.
    """
    try:
        from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env

        bindings = provider_bindings_from_env()
        if "openrouter" not in bindings.bound_providers:
            return None
        from sonder_runtime.adapters.inference.openrouter_gateway import (
            check_endpoint_policy,
            config_from_env,
        )

        settings = config_from_env()
        check_endpoint_policy(settings)
        tiers = sorted(
            tier for tier, provider in bindings.tier_providers.items()
            if provider == "openrouter"
        )
        missing = [tier for tier in tiers if not settings.model_for_tier(tier)]
        if missing:
            return CheckResult(
                "openrouter", False, False,
                "no OpenRouter model for tier(s) %s; run `python -m sonder_runtime "
                "openrouter use <tier> <vendor/model>`" % ", ".join(missing),
            )
        return CheckResult(
            "openrouter", True, False,
            "hosted provider configured for %s (not probed)" % (", ".join(tiers) or "default"),
        )
    except Exception as exc:  # noqa: BLE001 - preflight never blocks on a hosted provider
        return CheckResult("openrouter", False, False, str(exc)[:240])


def _check_anthropic() -> CheckResult | None:
    """Configuration-only note for a bound Anthropic provider.

    Never contacts Anthropic and never names a key's value: it reports the
    cloud opt-in, which key slots are set, and that the optional SDK is present.
    """
    try:
        from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env

        if "anthropic" not in provider_bindings_from_env().bound_providers:
            return None
        from sonder_runtime.adapters.inference.anthropic_gateway import (
            check_endpoint_policy,
            config_from_env,
            load_sdk,
        )

        settings = config_from_env()
        check_endpoint_policy(settings)
        load_sdk()
        return CheckResult(
            "anthropic", True, False,
            "hosted provider configured, key slot(s) %s, daily cap $%s (not probed)" % (
                "+".join(str(slot) for slot, _key in settings.key_slots), settings.daily_usd,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - preflight never blocks on a hosted provider
        return CheckResult("anthropic", False, False, str(exc)[:240])


def run_preflight(
    config: SonderConfig,
    *,
    check_ollama: bool = True,
    ollama_timeout: float = 5.0,
) -> PreflightReport:
    checks: list[CheckResult] = []
    checks.extend(_check_state_directories(config))
    checks.append(_check_disk_space(config))
    checks.extend(_check_schema_versions(config))
    checks.append(_check_runtime_policy())
    if check_ollama:
        ca_bundle = getattr(getattr(config, "inference", None), "ca_bundle", "") or getattr(
            getattr(config, "ollama", None), "ca_bundle", "",
        )
        try:
            ollama_endpoint.configure_typed_ca_bundle(ca_bundle)
        except ValueError as exc:
            checks.append(CheckResult("inference_ca_bundle", False, False, str(exc)))
        checks.append(_check_inference(config, timeout=ollama_timeout))
        checks.extend(_check_ollama_workers(config, timeout=ollama_timeout))
    router = _check_openrouter()
    if router is not None:
        checks.append(router)
    claude = _check_anthropic()
    if claude is not None:
        checks.append(claude)
    return PreflightReport(checks=tuple(checks))
