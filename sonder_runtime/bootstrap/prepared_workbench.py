"""Private exact-plan adapter; prepared work is data, never authority."""

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import threading

from ..application.context import OperationContext
from ..application.ports.app_managed_work import (
    PreparedWorkbenchRun,
    WorkSpec,
    canonical_digest,
)
from ..application.routing import tier_escalation
from ..interfaces import standalone_agent_lanes

_CURRENT = ContextVar("private_prepared_workbench", default=None)
_AUTO = frozenset(("auto", "default", "policy", ""))


def current_permit():
    return _CURRENT.get()


def prepared_target(prompt, tier, max_steps, allow_web, project, allow_location):
    permit = _CURRENT.get()
    if permit is None:
        return None
    state = permit.require_current()
    prepared, context, plan = state[:3]
    if (
        standalone_agent_lanes._LOOP_DEPTH.get() != 1
        or prompt != prepared.spec.prompt
        or max_steps != prepared.spec.max_steps
        or allow_web is not prepared.spec.allow_web
        or allow_location is not prepared.allow_location
        or project != prepared.project_root
    ):
        raise PermissionError("prepared work invocation differs from admitted options")
    controller = standalone_agent_lanes.current()
    if controller is None:
        raise PermissionError("prepared work requires its managed controller")
    controller.require_current()
    rung = next((r for r in plan.rungs if r.tier == tier), None)
    if rung is None:
        raise PermissionError("unprepared model tier")
    return rung.model, rung.cloud, rung.augment, rung.tier


def prepared_tool_allowlist(requested):
    permit = _CURRENT.get()
    if permit is None:
        return requested
    state = permit.require_current()
    allowed = frozenset(state[3])
    if requested is not None:
        allowed &= frozenset(
            permit.adapter.runtime._canonical_agent_tool_name(name)
            for name in requested
        )
    return allowed


def require_prepared_tool(tool_name):
    permit = _CURRENT.get()
    if permit is None:
        return
    state = permit.require_current()
    name = permit.adapter.runtime._canonical_agent_tool_name(tool_name)
    if name not in state[3]:
        raise PermissionError("tool is outside the prepared host ceiling")


@dataclass(frozen=True, eq=False)
class _Permit:
    adapter: object
    key: object

    def require_current(self):
        return self.adapter._require(self.key)


class PreparedWorkbenchAdapter:
    def __init__(self, runtime, *, policy_snapshot):
        if not callable(policy_snapshot):
            raise TypeError("live host policy snapshot required")
        self.runtime = runtime
        self.policy_snapshot = policy_snapshot
        self._active = {}
        self._lock = threading.RLock()

    @staticmethod
    def _context(context):
        if (
            type(context) is not OperationContext
            or context.deadline_monotonic is None
            or not math.isfinite(context.deadline_monotonic)
            or context.expired
            or context.cancellation.cancelled
            or type(context.workspace_roots) is not tuple
            or len(context.workspace_roots) != 1
        ):
            raise PermissionError("one live bounded admitted project context required")
        root = context.workspace_roots[0]
        if (
            not isinstance(root, Path)
            or not root.is_absolute()
            or root.resolve() != root
            or not root.is_dir()
        ):
            raise PermissionError("canonical existing admitted project required")
        return str(root)

    def _resolve(self, spec, context, allow_location):
        root = self._context(context)
        runtime = self.runtime
        if runtime.unsafe_lab.active():
            raise PermissionError("prepared work cannot run under unsafe overrides")
        # Detach the trusted snapshot before comparing or handing it to any callback.
        encoded = json.dumps(
            self.policy_snapshot(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode()) > 131072:
            raise ValueError("host policy snapshot exceeds bound")
        policy = json.loads(encoded)
        if (
            type(policy) is not dict
            or type(policy.get("allow_web")) is not bool
            or type(policy.get("allow_location")) is not bool
            or type(policy.get("allowed_tools")) is not list
            or len(policy["allowed_tools"]) > 256
            or any(
                type(t) is not str or not 1 <= len(t.encode()) <= 128
                for t in policy["allowed_tools"]
            )
            or spec.allow_web
            and not policy["allow_web"]
            or allow_location
            and not policy["allow_location"]
        ):
            raise PermissionError("explicit current web/location/tool ceiling required")
        tiers = dict(runtime.TIERS)
        runtime._refresh_live_cloud_tiers_policy(
            tiers,
            runtime.os.environ,
            default_cloud_general_model=runtime._RUNTIME_MODEL_CONFIGURATION.default_cloud_general_model,
        )
        cloud_enabled = runtime._cloud_allowed_policy(runtime.os.environ)
        if not 1 <= len(tiers) <= 128 or not runtime._RUNTIME_POLICY:
            raise PermissionError("configured model policy unavailable")
        requested = spec.tier
        tier = runtime._runtime_lane_tier("workbench", requested)
        model = tiers.get(tier)
        if type(model) is not str or not model:
            raise PermissionError("prepared work requires a configured concrete tier")
        cloud = runtime._is_cloud_tier(tier, model)
        if cloud and (not context.cloud_allowed or not cloud_enabled):
            raise PermissionError("cloud work exceeds admitted context")
        if (
            not cloud
            and not runtime._ollama_endpoint_is_local()
            and not context.remote_ollama_allowed
        ):
            raise PermissionError("remote inference exceeds admitted context")
        start = tier_escalation.Rung(tier, model, cloud=cloud, augment=tier == "code")
        plan = (
            runtime._default_route_plan(spec.prompt, start)
            if requested == "auto"
            else tier_escalation.single(start)
        )
        if any(r.cloud and not context.cloud_allowed for r in plan.rungs):
            raise PermissionError("prepared ladder exceeds cloud ceiling")
        allowed_tools = tuple(
            sorted(
                set(
                    runtime._canonical_agent_tool_name(name)
                    for name in policy["allowed_tools"]
                )
            )
        )
        snapshot = dict(
            schema=1,
            policy=policy,
            canonical_tools=allowed_tools,
            tiers=tiers,
            runtime_policy=runtime._RUNTIME_POLICY,
            escalation=runtime._model_escalation_enabled(),
            endpoint_local=runtime._ollama_endpoint_is_local(),
            endpoint=runtime.BASE,
            workers=runtime.OLLAMA_POOL.origins,
            cloud_enabled=cloud_enabled,
            prompt_digest=canonical_digest(spec.prompt),
            plan=asdict(plan),
            root=root,
            requested=requested,
            max_steps=spec.max_steps,
            allow_web=spec.allow_web,
            allow_location=allow_location,
            principal=context.principal_id,
            source=context.source,
            auth=context.auth_level,
            cloud=context.cloud_allowed,
            remote=context.remote_ollama_allowed,
        )
        return plan, canonical_digest(snapshot), allowed_tools

    def prepare_workbench(self, request, admitted_context):
        if type(request) is not dict or set(request) - {
            "prompt",
            "tier",
            "max_steps",
            "allow_web",
            "allow_location",
        }:
            raise ValueError("exact bounded work options required")
        requested = request.get("tier", "auto")
        if type(requested) is not str or len(requested) > 128:
            raise ValueError("bounded requested tier required")
        requested = requested.strip().lower()
        requested = "auto" if requested in _AUTO else requested
        steps = request.get("max_steps", 12)
        if type(steps) is not int or not 1 <= steps <= 64:
            raise ValueError("bounded work step count required")
        web = request.get("allow_web", True)
        location = request.get("allow_location", False)
        if type(web) is not bool or type(location) is not bool:
            raise ValueError("explicit boolean work options required")
        spec = WorkSpec(
            request.get("prompt"), requested, "unresolved", min(steps, 20), web
        )
        plan, policy_digest, _tools = self._resolve(spec, admitted_context, location)
        spec = WorkSpec(spec.prompt, requested, plan.start.model, spec.max_steps, web)
        return PreparedWorkbenchRun(
            spec,
            self._context(admitted_context),
            tuple(r.model for r in plan.rungs),
            policy_digest,
            location,
        )

    def _validate(self, prepared, context):
        if type(prepared) is not PreparedWorkbenchRun:
            raise PermissionError("typed prepared workbench run required")
        prepared.__post_init__()
        plan, digest, tools = self._resolve(
            prepared.spec, context, prepared.allow_location
        )
        if (
            digest != prepared.policy_digest
            or tuple(r.model for r in plan.rungs) != prepared.model_ladder
            or plan.start.model != prepared.spec.resolved_model
            or self._context(context) != prepared.project_root
        ):
            raise PermissionError("prepared work policy or model plan changed")
        return plan, tools

    def _require(self, key):
        with self._lock:
            state = self._active.get(key)
        if state is None:
            raise PermissionError("prepared work invocation is no longer active")
        self._validate(state[0], state[1])
        return state

    def execute_prepared_workbench(
        self, prepared, *, admitted_context, managed_factory
    ):
        if not callable(managed_factory) or _CURRENT.get() is not None:
            raise PermissionError("private unnested managed factory required")
        plan, tools = self._validate(prepared, admitted_context)
        key = object()
        permit = _Permit(self, key)
        with self._lock:
            if len(self._active) >= 32:
                raise PermissionError("prepared work invocation capacity unavailable")
            self._active[key] = (prepared, admitted_context, plan, tools)

        def factory(controller, application):
            permit.require_current()
            session = managed_factory(controller, application)
            try:
                context = session.context
                if (
                    context.principal_id != admitted_context.principal_id
                    or context.source != admitted_context.source
                    or context.auth_level != admitted_context.auth_level
                    or context.cancellation is not admitted_context.cancellation
                    or context.workspace_roots != admitted_context.workspace_roots
                    or context.deadline_monotonic is None
                    or context.deadline_monotonic > admitted_context.deadline_monotonic
                    or context.cloud_allowed != admitted_context.cloud_allowed
                    or context.remote_ollama_allowed
                    != admitted_context.remote_ollama_allowed
                ):
                    raise PermissionError(
                        "managed factory context exceeds prepared admission"
                    )
                permit.require_current()
                return session
            except BaseException:
                session.close()
                raise

        token = _CURRENT.set(permit)
        try:
            with standalone_agent_lanes.managed_controller_factory_scope(factory):
                options = dict(
                    max_steps=prepared.spec.max_steps,
                    allow_web=prepared.spec.allow_web,
                    project=prepared.project_root,
                    allow_location=prepared.allow_location,
                )
                if prepared.spec.tier == "auto":
                    return self.runtime._workbench_agent_escalating(
                        prepared.spec.prompt,
                        plan.start.tier,
                        prepared_plan=plan,
                        **options
                    )[0]
                return self.runtime.agent(
                    prompt=prepared.spec.prompt, tier=plan.start.tier, **options
                )
        finally:
            _CURRENT.reset(token)
            with self._lock:
                self._active.pop(key, None)
