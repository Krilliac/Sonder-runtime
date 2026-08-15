"""Typed, deterministic, fail-closed production configuration loader.

SPEC-2 section 3: this module is the sole production configuration source.
Precedence, lowest to highest:

    built-in safe defaults
    < profile TOML
    < secrets environment file
    < process environment (compatibility variables)
    < explicit command-line overrides

Secrets are never accepted in TOML — a secret key in the TOML file is a
validation error, not a convenience.  Validation collects *all* errors so
an operator fixes a configuration in one pass, and no caller may open a
listener until ``load_config`` returns without errors.

The hot-reloadable runtime policy (runtime_policy.py) stays separate on
purpose: it may pick model aliases and routing lanes but can never widen
network, filesystem, credential, or cloud permissions.
"""
from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import urlsplit

import sonder_paths
import unsafe_lab

PROFILES = ("workstation-local", "server-private")

# Keys that must only ever arrive through the secrets environment file or
# process environment.  Their presence in TOML fails validation.
SECRET_ENV_KEYS = (
    "SONDER_API_KEY",
    "SONDER_AUTH_SECRET",
    "SONDER_BACKUP_KEY_FILE",
    "SONDER_LAUNCHER_HEALTH_TOKEN",
)
_SECRET_TOML_KEYS = frozenset(
    {"api_key", "auth_secret", "backup_key", "backup_key_file", "secret", "token"}
)

MIN_API_KEY_LENGTH = 24

# Auth modes that mint/verify account session tokens (as opposed to the plain
# API-key or local-open profiles).  These key an HMAC with the auth secret.
ACCOUNT_BEARING_AUTH_MODES = ("account", "both", "either")

# The historical public development secret.  admin_auth no longer falls back to
# it, but if an operator has pinned it explicitly we refuse to start any
# account-bearing mode with it (belt-and-suspenders with the >=32-char rule).
BUILTIN_DEV_AUTH_SECRET = "sonder-local-dev-secret"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 11435
    auth_mode: str = "api-key"
    max_request_bytes: int = 1_048_576
    max_concurrent_requests: int = 4
    request_timeout_seconds: int = 300
    stream_idle_timeout_seconds: int = 60
    cors_origins: tuple[str, ...] = ()
    require_account: bool = False
    allow_registration: bool = False
    reasoning_audience: str = "developer"
    session_state_limit: int = 128
    session_state_owner_limit: int = 32
    train_max_n: int = 500
    trusted_proxy_cidrs: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    # Explicit operator declaration that a TLS-terminating proxy fronts any
    # non-loopback exposure.  Loopback binding never needs it; non-loopback
    # binding is rejected without it.
    tls_terminated_by_proxy: bool = False


@dataclass(frozen=True)
class StateConfig:
    home: str = ""
    workspace_roots: tuple[str, ...] = ()
    minimum_free_disk_bytes: int = 5_368_709_120
    sqlite_busy_timeout_ms: int = 5_000


@dataclass(frozen=True)
class OllamaConfig:
    url: str = "http://127.0.0.1:11434"
    allow_remote: bool = False
    startup_timeout_seconds: int = 60
    request_timeout_seconds: int = 300


@dataclass(frozen=True)
class FeaturesConfig:
    cloud: bool = False
    web: bool = False
    live_reload: bool = False
    source_modification: bool = False
    host_control: bool = False
    training: bool = False
    npu: bool = False
    expose_reasoning: bool = False
    allow_private_cot: bool = False
    location_consent: bool = False


@dataclass(frozen=True)
class CapacityConfig:
    model_generations: int = 1
    http_requests: int = 4
    tool_processes: int = 2
    fleet_workers: int = 2
    autopilot_runs: int = 1
    training_jobs: int = 1
    queue_depth: int = 32


@dataclass(frozen=True)
class ObservabilityConfig:
    log_format: str = "json"
    log_level: str = "INFO"
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    audit_retention_days: int = 90


@dataclass(frozen=True)
class BackupConfig:
    enabled: bool = True
    target: str = ""
    retention_daily: int = 7
    retention_weekly: int = 4
    retention_monthly: int = 6


@dataclass(frozen=True)
class Secrets:
    """Secret material, loaded separately so it can never be dumped."""

    api_key: str = ""
    auth_secret: str = ""
    backup_key_file: str = ""

    def as_redacted_dict(self) -> dict:
        return {
            "api_key": _redact_presence(self.api_key),
            "auth_secret": _redact_presence(self.auth_secret),
            "backup_key_file": self.backup_key_file or "[unset]",
        }


@dataclass(frozen=True)
class SonderConfig:
    schema_version: int = 1
    profile: str = "workstation-local"
    server: ServerConfig = field(default_factory=ServerConfig)
    state: StateConfig = field(default_factory=StateConfig)
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    features: FeaturesConfig = field(default_factory=FeaturesConfig)
    capacity: CapacityConfig = field(default_factory=CapacityConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    secrets: Secrets = field(default_factory=Secrets)
    # Provenance for diagnostics: which layers actually contributed.
    sources: tuple[str, ...] = ()

    def as_redacted_dict(self) -> dict:
        out: dict = {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "sources": list(self.sources),
        }
        for section in (
            "server",
            "state",
            "ollama",
            "features",
            "capacity",
            "observability",
            "backup",
        ):
            value = getattr(self, section)
            out[section] = {
                f.name: (
                    list(v) if isinstance(v := getattr(value, f.name), tuple) else v
                )
                for f in fields(value)
            }
        out["secrets"] = self.secrets.as_redacted_dict()
        return out


class ConfigError(ValueError):
    """All validation problems for one load attempt, reported together."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(
            "invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors)
        )
        self.errors = tuple(errors)


def _redact_presence(value: str) -> str:
    return "[set]" if value else "[unset]"


def _is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE secrets file (comments and blank lines allowed)."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(
                [f"{path}:{lineno}: expected KEY=VALUE, got {line[:32]!r}"]
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _walk_toml_for_secrets(data, path: str, errors: list[str]) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            where = f"{path}.{key}" if path else key
            if key.lower() in _SECRET_TOML_KEYS:
                errors.append(
                    f"secret value {where!r} may not appear in TOML; "
                    "use the secrets environment file"
                )
            _walk_toml_for_secrets(value, where, errors)


_SECTION_TYPES = {
    "server": ServerConfig,
    "state": StateConfig,
    "ollama": OllamaConfig,
    "features": FeaturesConfig,
    "capacity": CapacityConfig,
    "observability": ObservabilityConfig,
    "backup": BackupConfig,
}


def _apply_section(current, section_name: str, raw: dict, errors: list[str]):
    cls = _SECTION_TYPES[section_name]
    known = {f.name: f for f in fields(cls)}
    updates = {}
    for key, value in raw.items():
        if key in _SECRET_TOML_KEYS:
            continue  # already reported by the secrets walk
        if key not in known:
            errors.append(f"unknown key [{section_name}].{key}")
            continue
        expected = type(getattr(current, key))
        if expected is tuple:
            if not isinstance(value, list) or not all(
                isinstance(v, str) for v in value
            ):
                errors.append(f"[{section_name}].{key} must be a list of strings")
                continue
            updates[key] = tuple(value)
        elif expected is bool:
            if not isinstance(value, bool):
                errors.append(f"[{section_name}].{key} must be a boolean")
                continue
            updates[key] = value
        elif expected is int:
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"[{section_name}].{key} must be an integer")
                continue
            updates[key] = value
        else:
            if not isinstance(value, str):
                errors.append(f"[{section_name}].{key} must be a string")
                continue
            updates[key] = value
    return replace(current, **updates) if updates else current


def _env_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, env: dict[str, str], current: int, errors: list[str]) -> int:
    """Read one compatibility integer without letting malformed env bypass TOML."""
    raw = env.get(name, "").strip()
    if not raw:
        return current
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name} is not an integer")
        return current


def _apply_environment(
    config: SonderConfig, env: dict[str, str], errors: list[str]
) -> SonderConfig:
    """Fold the existing compatibility environment variables in."""
    server = config.server
    state = config.state
    ollama = config.ollama
    features = config.features
    secrets = config.secrets

    if env.get("SONDER_HOST", "").strip():
        server = replace(server, host=env["SONDER_HOST"].strip())
    if env.get("SONDER_PORT", "").strip():
        try:
            server = replace(server, port=int(env["SONDER_PORT"].strip()))
        except ValueError:
            errors.append(f"SONDER_PORT is not an integer: {env['SONDER_PORT']!r}")
    if env.get("SONDER_AUTH_MODE", "").strip():
        server = replace(server, auth_mode=env["SONDER_AUTH_MODE"].strip())
    if env.get("SONDER_MAX_REQUEST_BYTES", "").strip():
        try:
            server = replace(
                server, max_request_bytes=int(env["SONDER_MAX_REQUEST_BYTES"])
            )
        except ValueError:
            errors.append("SONDER_MAX_REQUEST_BYTES is not an integer")
    server = replace(
        server,
        max_concurrent_requests=_env_int(
            "SONDER_MAX_CONCURRENT_REQUESTS", env,
            server.max_concurrent_requests, errors,
        ),
        request_timeout_seconds=_env_int(
            "SONDER_REQUEST_TIMEOUT_SECONDS", env,
            server.request_timeout_seconds, errors,
        ),
        stream_idle_timeout_seconds=_env_int(
            "SONDER_STREAM_IDLE_TIMEOUT_SECONDS", env,
            server.stream_idle_timeout_seconds, errors,
        ),
        session_state_limit=_env_int(
            "SONDER_HTTP_SESSION_STATE_LIMIT", env,
            server.session_state_limit, errors,
        ),
        session_state_owner_limit=_env_int(
            "SONDER_HTTP_SESSION_STATE_OWNER_LIMIT", env,
            server.session_state_owner_limit, errors,
        ),
        train_max_n=_env_int("SONDER_TRAIN_MAX_N", env, server.train_max_n, errors),
    )
    if "SONDER_CORS_ORIGINS" in env:
        server = replace(
            server,
            cors_origins=tuple(
                part.strip() for part in env["SONDER_CORS_ORIGINS"].split(",")
                if part.strip()
            ),
        )
    if "SONDER_REQUIRE_ACCOUNT" in env:
        server = replace(server, require_account=_env_bool(env["SONDER_REQUIRE_ACCOUNT"]))
    if "SONDER_ALLOW_REGISTRATION" in env:
        server = replace(
            server, allow_registration=_env_bool(env["SONDER_ALLOW_REGISTRATION"])
        )
    if env.get("SONDER_REASONING_AUDIENCE", "").strip():
        server = replace(
            server, reasoning_audience=env["SONDER_REASONING_AUDIENCE"].strip().lower()
        )
    if env.get("SONDER_HOME", "").strip():
        state = replace(state, home=env["SONDER_HOME"].strip())
    if env.get("SONDER_FILE_ROOTS", "").strip():
        roots = tuple(
            part.strip()
            for part in env["SONDER_FILE_ROOTS"].split(os.pathsep)
            if part.strip()
        )
        if roots:
            state = replace(state, workspace_roots=roots)
    if env.get("OLLAMA_HOST", "").strip():
        raw = env["OLLAMA_HOST"].strip()
        url = raw if "://" in raw else f"http://{raw}"
        ollama = replace(ollama, url=url)
    if "SONDER_ALLOW_REMOTE_OLLAMA" in env:
        ollama = replace(
            ollama, allow_remote=_env_bool(env["SONDER_ALLOW_REMOTE_OLLAMA"])
        )
    if "SONDER_ALLOW_CLOUD" in env:
        features = replace(features, cloud=_env_bool(env["SONDER_ALLOW_CLOUD"]))
    if "SONDER_WEB_TOOLS" in env:
        features = replace(features, web=_env_bool(env["SONDER_WEB_TOOLS"]))
    if "SONDER_LIVE_RELOAD" in env:
        features = replace(
            features, live_reload=_env_bool(env["SONDER_LIVE_RELOAD"])
        )
    if "SONDER_EXPOSE_REASONING" in env:
        features = replace(
            features, expose_reasoning=_env_bool(env["SONDER_EXPOSE_REASONING"])
        )
    if "SONDER_ALLOW_PRIVATE_COT" in env:
        features = replace(
            features, allow_private_cot=_env_bool(env["SONDER_ALLOW_PRIVATE_COT"])
        )
    if "SONDER_LOCATION_CONSENT" in env:
        features = replace(
            features, location_consent=_env_bool(env["SONDER_LOCATION_CONSENT"])
        )
    if env.get("SONDER_API_KEY", "").strip():
        secrets = replace(secrets, api_key=env["SONDER_API_KEY"].strip())
    if env.get("SONDER_AUTH_SECRET", "").strip():
        secrets = replace(secrets, auth_secret=env["SONDER_AUTH_SECRET"].strip())
    if env.get("SONDER_BACKUP_KEY_FILE", "").strip():
        secrets = replace(
            secrets, backup_key_file=env["SONDER_BACKUP_KEY_FILE"].strip()
        )

    return replace(
        config,
        server=server,
        state=state,
        ollama=ollama,
        features=features,
        secrets=secrets,
    )


def _validate(config: SonderConfig, errors: list[str]) -> None:
    if config.schema_version != 1:
        errors.append(
            f"unsupported configuration schema_version {config.schema_version}"
        )
    if config.profile not in PROFILES:
        errors.append(
            f"unknown profile {config.profile!r}; expected one of {PROFILES}"
        )

    server = config.server
    if not 1 <= server.port <= 65_535:
        errors.append(f"[server].port out of range: {server.port}")
    if server.auth_mode not in ("api-key", "account", "both", "either"):
        errors.append(f"[server].auth_mode invalid: {server.auth_mode!r}")
    if server.max_request_bytes <= 0 or server.max_request_bytes > 16 * 1024 * 1024:
        errors.append("[server].max_request_bytes must be within 1..16MiB")
    if server.max_concurrent_requests < 1:
        errors.append("[server].max_concurrent_requests must be >= 1")
    if server.request_timeout_seconds < 1:
        errors.append("[server].request_timeout_seconds must be >= 1")
    if server.stream_idle_timeout_seconds < 1:
        errors.append("[server].stream_idle_timeout_seconds must be >= 1")
    if not 2 <= server.session_state_limit <= 1024:
        errors.append("[server].session_state_limit must be within 2..1024")
    if not 1 <= server.session_state_owner_limit < server.session_state_limit:
        errors.append(
            "[server].session_state_owner_limit must be within "
            "1..session_state_limit-1"
        )
    if server.train_max_n < 1:
        errors.append("[server].train_max_n must be >= 1")
    if server.reasoning_audience not in ("developer", "all"):
        errors.append("[server].reasoning_audience must be developer or all")
    for cidr in server.trusted_proxy_cidrs:
        try:
            ipaddress.ip_network(cidr)
        except ValueError:
            errors.append(f"[server].trusted_proxy_cidrs entry invalid: {cidr!r}")

    loopback = _is_loopback_host(server.host)
    if not loopback:
        # SPEC-2 remote exposure rules: non-loopback binding requires an
        # explicit TLS-proxy declaration AND strong authentication.  There
        # is no override; the reference topology keeps Sonder on loopback.
        if not server.tls_terminated_by_proxy:
            errors.append(
                f"[server].host {server.host!r} is not loopback: non-loopback "
                "binding without tls_terminated_by_proxy=true is prohibited"
            )
        if len(config.secrets.api_key) < MIN_API_KEY_LENGTH:
            errors.append(
                "non-loopback binding requires SONDER_API_KEY of at least "
                f"{MIN_API_KEY_LENGTH} characters in the secrets file"
            )
    if config.profile == "server-private":
        if len(config.secrets.api_key) < MIN_API_KEY_LENGTH:
            errors.append(
                "profile server-private requires SONDER_API_KEY of at least "
                f"{MIN_API_KEY_LENGTH} characters"
            )

    effective_auth_mode = (
        "account"
        if server.require_account and server.auth_mode == "api-key"
        else server.auth_mode
    )
    if (
        effective_auth_mode in ACCOUNT_BEARING_AUTH_MODES
        and config.secrets.auth_secret == BUILTIN_DEV_AUTH_SECRET
    ):
        errors.append(
            f"[server].auth_mode {server.auth_mode!r} may not use the built-in "
            "development auth secret; set SONDER_AUTH_SECRET to a private value"
        )

    if config.state.minimum_free_disk_bytes < 0:
        errors.append("[state].minimum_free_disk_bytes must be >= 0")
    if config.state.sqlite_busy_timeout_ms < 0:
        errors.append("[state].sqlite_busy_timeout_ms must be >= 0")
    for root in config.state.workspace_roots:
        if not Path(root).expanduser().is_absolute():
            errors.append(f"[state].workspace_roots entry not absolute: {root!r}")

    parts = urlsplit(config.ollama.url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        errors.append(f"[ollama].url invalid: {config.ollama.url!r}")
    elif not config.ollama.allow_remote and not _is_loopback_host(parts.hostname):
        errors.append(
            f"[ollama].url host {parts.hostname!r} is not loopback and "
            "allow_remote is false (remote-Ollama consent gate)"
        )
    elif not _is_loopback_host(parts.hostname) and parts.scheme != "https":
        errors.append(
            "[ollama].url remote Ollama must use https so prompts and "
            "embeddings are protected in transit"
        )

    for name in ("model_generations", "http_requests", "tool_processes",
                 "fleet_workers", "autopilot_runs", "training_jobs",
                 "queue_depth"):
        if getattr(config.capacity, name) < 1:
            errors.append(f"[capacity].{name} must be >= 1")

    obs = config.observability
    if obs.log_format not in ("json", "text"):
        errors.append(f"[observability].log_format invalid: {obs.log_format!r}")
    if obs.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append(f"[observability].log_level invalid: {obs.log_level!r}")
    if obs.audit_retention_days < 1:
        errors.append("[observability].audit_retention_days must be >= 1")
    if not obs.metrics_path.startswith("/"):
        errors.append("[observability].metrics_path must start with '/'")

    if config.backup.enabled and config.backup.target:
        if not Path(config.backup.target).expanduser().is_absolute():
            errors.append(
                f"[backup].target must be an absolute path: {config.backup.target!r}"
            )
    for name in ("retention_daily", "retention_weekly", "retention_monthly"):
        if getattr(config.backup, name) < 1:
            errors.append(f"[backup].{name} must be >= 1")


def load_config(
    toml_path: str | os.PathLike | None = None,
    *,
    secrets_path: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
) -> SonderConfig:
    """Load, merge, and validate configuration; raise ConfigError on any fault.

    ``overrides`` carries explicit command-line values (highest precedence)
    as flat dotted keys, e.g. ``{"server.port": "12000"}``.
    """
    errors: list[str] = []
    sources: list[str] = ["defaults"]
    config = SonderConfig()

    if toml_path is not None:
        path = Path(toml_path)
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError([f"configuration file not found: {path}"]) from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError([f"{path}: TOML parse error: {exc}"]) from None
        _walk_toml_for_secrets(raw, "", errors)
        sources.append(str(path))
        for key, value in raw.items():
            if key == "schema_version":
                if isinstance(value, int) and not isinstance(value, bool):
                    config = replace(config, schema_version=value)
                else:
                    errors.append("schema_version must be an integer")
            elif key == "profile":
                if isinstance(value, str):
                    config = replace(config, profile=value)
                else:
                    errors.append("profile must be a string")
            elif key in _SECTION_TYPES:
                if isinstance(value, dict):
                    config = replace(
                        config,
                        **{
                            key: _apply_section(
                                getattr(config, key), key, value, errors
                            )
                        },
                    )
                else:
                    errors.append(f"[{key}] must be a table")
            else:
                errors.append(f"unknown top-level key {key!r}")

    merged_env: dict[str, str] = {}
    if secrets_path is not None:
        spath = Path(secrets_path)
        if not spath.exists():
            errors.append(f"secrets file not found: {spath}")
        else:
            if os.name == "posix":
                mode = spath.stat().st_mode & 0o777
                if mode & 0o077:
                    errors.append(
                        f"secrets file {spath} must not be group/world "
                        f"accessible (mode is {oct(mode)})"
                    )
            try:
                merged_env.update(parse_env_file(spath))
                sources.append(str(spath))
            except ConfigError as exc:
                errors.extend(exc.errors)

    process_env = dict(os.environ) if env is None else dict(env)
    merged_env.update(process_env)
    if any(k.startswith(("SONDER_", "OLLAMA_")) for k in process_env):
        sources.append("environment")
    config = _apply_environment(config, merged_env, errors)

    if overrides:
        sources.append("command-line")
        config = _apply_overrides(config, overrides, errors)

    # Check the final effective listener host, including the highest-precedence
    # CLI overrides. Otherwise an exact unsafe acknowledgement plus
    # --host=0.0.0.0 could evade the lab's stricter loopback-only rule.
    lab_state = unsafe_lab.inspect(env=merged_env, host=config.server.host)
    if lab_state.error:
        errors.append(lab_state.error)

    if not config.state.home:
        config = replace(
            config, state=replace(config.state, home=str(sonder_paths.default_home()))
        )

    _validate(config, errors)
    if errors:
        raise ConfigError(errors)
    return replace(config, sources=tuple(sources))


_OVERRIDE_PATTERN = re.compile(r"^[a-z_]+\.[a-z_]+$")


def _apply_overrides(
    config: SonderConfig, overrides: dict[str, str], errors: list[str]
) -> SonderConfig:
    for dotted, raw_value in overrides.items():
        if not _OVERRIDE_PATTERN.match(dotted):
            errors.append(f"invalid override key {dotted!r} (expected section.key)")
            continue
        section_name, key = dotted.split(".", 1)
        if section_name not in _SECTION_TYPES:
            errors.append(f"invalid override section {section_name!r}")
            continue
        section = getattr(config, section_name)
        if not hasattr(section, key):
            errors.append(f"invalid override key {dotted!r}")
            continue
        current = getattr(section, key)
        try:
            if isinstance(current, bool):
                value: object = _env_bool(raw_value)
            elif isinstance(current, int):
                value = int(raw_value)
            elif isinstance(current, tuple):
                value = tuple(
                    part.strip() for part in raw_value.split(",") if part.strip()
                )
            else:
                value = raw_value
        except ValueError:
            errors.append(f"override {dotted!r} has invalid value {raw_value!r}")
            continue
        config = replace(
            config, **{section_name: replace(section, **{key: value})}
        )
    return config
