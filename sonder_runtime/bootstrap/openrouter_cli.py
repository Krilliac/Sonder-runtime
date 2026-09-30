"""``python -m sonder_runtime openrouter ...`` -- the operator's OpenRouter surface.

``models``   list the models this key may use (id, context, price per 1M
             tokens, tools / structured-output support); read-only.
``account``  the key's limit, remaining limit and usage, plus the account
             credit balance when the key can read it; read-only.
``use``      map one Sonder tier to an OpenRouter model through the shared
             runtime-policy update path (the same lock, revision and
             deployment-transition guard as ``/runtime set``).

The two reads need the cloud opt-in (``SONDER_ALLOW_CLOUD=1``) because they
send the API key to OpenRouter; neither sends a prompt or spends credits.
``use`` is offline unless ``--verify`` asks it to confirm the id exists.
Nothing here prints, logs or stores the key.
"""
from __future__ import annotations

import json
import os
import sys

from ..adapters.inference.openrouter_gateway import (
    ENV_TIER_MODELS,
    PROVIDER_ID,
    OpenRouterGateway,
)
from ..adapters.provider_bindings import PROVIDER_TIERS, TIER_PROVIDER_ENV, provider_bindings_from_env
from ..domain.common.errors import SonderError
from ..domain.openrouter_policy import OpenRouterPolicyError, validate_model_id

_CLEAR_TOKENS = frozenset({"none", "off", "-", "unset"})


def _price(value: object) -> str:
    return "-" if not isinstance(value, (int, float)) else ("%.2f" % value if value >= 0.01 or value == 0 else "%.4f" % value)


def format_models(listing: dict) -> str:
    rows = listing.get("models") or []
    source = ("models available to this key" if listing.get("source") == "account"
              else "public catalog (not filtered by your account)")
    lines = ["OpenRouter %s: %d shown of %d" % (source, len(rows), listing.get("total", len(rows)))]
    if rows:
        width = min(60, max(len(str(row["id"])) for row in rows))
        lines.append("  %-*s %9s %9s %9s  %s" % (width, "id", "context", "$in/1M", "$out/1M", "features"))
        for row in rows:
            features = [name for name, flag in (
                ("tools", row.get("supports_tools")),
                ("structured", row.get("supports_structured_outputs")),
            ) if flag]
            lines.append("  %-*s %9s %9s %9s  %s" % (
                width, row["id"], row.get("context_length") or "-",
                _price(row.get("prompt_usd_per_million")),
                _price(row.get("completion_usd_per_million")),
                ",".join(features) or "-",
            ))
    return "\n".join(lines)


def format_account(info: dict) -> str:
    def money(value):
        return "unlimited" if value is None else "$%.4f" % value

    lines = ["OpenRouter account"]
    if info.get("credits_remaining") is not None:
        lines.append("  credits remaining: $%.4f (purchased $%.4f, used $%.4f)" % (
            info["credits_remaining"], info.get("total_credits") or 0.0, info.get("total_usage") or 0.0,
        ))
    elif info.get("credits_note"):
        lines.append("  credits: %s" % info["credits_note"])
    lines.append("  key limit: %s | remaining: %s" % (money(info.get("limit")), money(info.get("limit_remaining"))))
    lines.append("  key usage: total $%.4f | today $%.4f | week $%.4f | month $%.4f" % tuple(
        float(info.get(key) or 0.0) for key in ("usage", "usage_daily", "usage_weekly", "usage_monthly")
    ))
    if info.get("is_free_tier") is not None:
        lines.append("  free tier: %s" % ("yes" if info["is_free_tier"] else "no (credits purchased)"))
    return "\n".join(lines)


def apply_tier_model(tier: str, model: str, *, gateway: OpenRouterGateway | None = None,
                     verify: bool = False, env=None) -> dict:
    """Validate, optionally verify, then write through ``runtime_policy.update``."""
    from ..adapters import runtime_policy

    source = os.environ if env is None else env
    tier = str(tier or "").strip().lower()
    if tier not in PROVIDER_TIERS:
        raise ValueError("unknown tier %r (tiers: %s)" % (tier, ", ".join(PROVIDER_TIERS)))
    clearing = str(model or "").strip().lower() in _CLEAR_TOKENS
    if not clearing:
        model = validate_model_id(model, "model")
        if verify:
            listing = (gateway or OpenRouterGateway()).list_models(search=model)
            if model not in {row["id"] for row in listing["models"]}:
                raise ValueError("OpenRouter does not list %r for this key" % model)
    policy = runtime_policy.update(
        provider_models={PROVIDER_ID: {tier: "" if clearing else model}},
        source="openrouter use",
    )
    mapped = ((policy.get("provider_models") or {}).get(PROVIDER_ID) or {}).get(tier)
    notes = []
    if str(source.get(ENV_TIER_MODELS, "") or "").strip():
        notes.append("%s is set and overrides the policy for any tier it names" % ENV_TIER_MODELS)
    try:
        bound = provider_bindings_from_env(source).tier_providers.get(tier)
    except ValueError:
        bound = None
    if bound != PROVIDER_ID:
        notes.append(
            "tier %s is served by %s; set %s=openrouter (and SONDER_ALLOW_CLOUD=1) "
            "to route it to OpenRouter" % (tier, bound or "an invalid binding", TIER_PROVIDER_ENV[tier])
        )
    return {"tier": tier, "model": mapped, "policy_revision": policy.get("revision"), "notes": notes}


def cmd_openrouter(args) -> int:
    def emit(payload: dict, text: str) -> None:
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else text)

    try:
        if args.openrouter_command == "models":
            listing = OpenRouterGateway().list_models(search=args.search or "", tools=args.tools)
            emit(listing, format_models(listing))
        elif args.openrouter_command == "account":
            info = OpenRouterGateway().account()
            emit(info, format_account(info))
        else:
            result = apply_tier_model(args.tier, args.model, verify=args.verify)
            text = "openrouter tier %s -> %s (runtime policy revision %s)" % (
                result["tier"], result["model"] or "(cleared)", result["policy_revision"],
            )
            emit(result, "\n".join([text, *("  note: %s" % note for note in result["notes"])]))
    except (SonderError, OpenRouterPolicyError, ValueError, RuntimeError, OSError) as exc:
        print("openrouter: %s" % exc, file=sys.stderr)
        return 2
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser(
        "openrouter", help="OpenRouter models, account credits and tier mapping",
    )
    commands = parser.add_subparsers(dest="openrouter_command", required=True)
    models = commands.add_parser("models", help="list models available to this key (read-only)")
    models.add_argument("--search", default="", help="substring of the model id or name")
    models.add_argument("--tools", action="store_true", help="only models that support tool calling")
    models.add_argument("--json", action="store_true")
    account = commands.add_parser("account", help="credits, key limit and usage (read-only)")
    account.add_argument("--json", action="store_true")
    use = commands.add_parser("use", help="map a Sonder tier to an OpenRouter model (runtime policy)")
    use.add_argument("tier", choices=PROVIDER_TIERS)
    use.add_argument("model", help="vendor/model[:variant], or 'none' to clear the tier")
    use.add_argument("--verify", action="store_true",
                     help="confirm the model is listed for this key first (needs cloud opt-in)")
    use.add_argument("--json", action="store_true")
    parser.set_defaults(func=cmd_openrouter)


__all__ = ["add_parser", "apply_tier_model", "cmd_openrouter", "format_account", "format_models"]
