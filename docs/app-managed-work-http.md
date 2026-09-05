# Managed app work HTTP boundary

This slice adds three early app-control routes. They require the existing deployment-key mode, an explicit exact account session, the separate app-control credential, and the current selected canonical binding. Ordinary chat labels, model arguments, a remembered work ID, or pending approval metadata confer no authority.

| Request | Body | Response |
| --- | --- | --- |
| POST /v1/app-control/work | command_id, prompt; optional tier, max_steps, allow_web, allow_location | 200 with work metadata and the canonical prepare_work/COMMITTED metadata receipt |
| POST /v1/app-control/work/<64 lowercase hex>/execute | Empty JSON object | 202 with current retained work; only newly admitted work submits once |
| GET /v1/app-control/work/<64 lowercase hex> | None | 200 with scoped current retained metadata |

The common envelope is `{ok:true,work:{work_id,state,revision,project,expires_at,options}}`. Options contain only requested tier, bounded step count, web and location choices. Optional `pending` contains kind `verification_approval`, the exact existing pending identity and safe approval call evidence. Optional `interruption` is the bounded stable unknown record. Optional `completion.phase` preserves the distinction between original and later certification; a terminal state or phase alone is not a claim that the original work succeeded. Prompt, output, roots, account references, model configuration, approval nonces, raw credentials and private issuers are omitted.

An actual dispatch ASK returns 409 `APP_WORK_APPROVAL_PENDING` with the real ledger call identity. The operator must use the existing exact approval mechanism and explicitly repeat execute; no new approval endpoint is supplied here. A potentially consumed approval with an uncertain outcome returns 503 `APP_WORK_APPROVAL_UNKNOWN`. No automatic model or verification replay is introduced. Durable verification-pending metadata remains status data only.

Status and execute remain scoped to the ORIGINAL live account/control session and selected binding revision/epoch. A fresh login, new control session, reselected epoch, or changed grant does not impersonate that original scope. Cross-login history/reattachment and recovery require the separate reviewed recovery service and are not routed by this slice. Successful metadata preparation is not execution.

Transport, duplicate-header refusal, raw peer/origin policy, no redirects, the 16 KiB body bound before generic/model routing, bounded peer/global admission, and no-store/no-referrer responses reuse the existing app-control boundary. The work service adds two concurrent requests per account with a bounded active-only map. All unexpected errors are projected to stable public codes.

Initial account/control authentication is released before dispatcher, model, approval, verifier or lifetime operations. Before writing a response, a separate fresh account admission checks the original exact account reference, live control credential, current catalog and exact binding/selection tuple. It does not use a worker-owned selection object as renewed authority. Idle request selections release before response I/O; accepted worker ownership remains with the dispatcher. Socket publication is attempted once. Revocation after a committed prepare withholds its metadata, without replaying the commit.

Contexts inherit the original finite account/control/grant expiry rather than the HTTP request timeout. Execute of prepared work is additionally bounded by its original work expiry. The first transport slice is local-model only: cloud and remote inference flags remain false even if a catalog carries broader ceilings. Web/location require both an explicit request and current host feature/tool policy. Project policy resolves exactly one matching principal/root catalog entry; ambiguous mappings refuse. Model/tool admission still uses the real private managed authority, not that policy lookup.

Production startup calls `install_owned_work_http(control, application=..., runtime=..., permission_engine=...)` only after the exact owned app-work slot exists and before listener publication. It constructs the real authority, prepared adapter, pinned approval bridge, private output codec/lifetime and current terminal eligibility service, then registers the exact dispatcher/Application identity before publishing the binding. Requests only read that installed binding; they never construct a dispatcher. Missing owned registration is unavailable. The owned-runtime slot must drain this dispatcher before lane/provider/storage shutdown; a local dispatcher drain is not native process cleanup proof.

The current foreground managed profile synthesizes app-control-disabled configuration. These routes do not silently enable it or claim production readiness. Actual typed enabled configuration and the separately reviewed owned slot remain prerequisites. No installed runtime or public deployment is changed by these tests.
