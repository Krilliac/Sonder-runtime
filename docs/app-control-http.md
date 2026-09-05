# App control HTTP vertical contract

Dedicated private routes under /v1/app-control. This slice enrolls and manages durable host-conversation metadata, and reads scoped recovery metadata. It does not grant account lane execution, attach a controller, spend verification approval, or automatically resume work. Existing account lane execution refusal remains active.

## Authentication and transport

All non-OPTIONS requests require exactly one X-Sonder-Account-Token containing the live account token, with current admin role and project catalog membership. Enrollment additionally checks the password against that same account session. Deployment Authorization is independently required when the typed deployment mode/key requires it. Other routes require exactly one X-Sonder-App-Control. Never use the account token as a deployment key or a control token.

The app-control token is sac1.<32 lowercase hex session ID>.<43 URL-safe random characters>. Keep it only in memory, scoped to the exact server origin/account/runtime. Do not put it in shared preferences, secure persistent storage, chat/model history, logs or a receipt. It is returned only on the first successful enrollment publication; the store has only salted verifier metadata. No automatic fallback or redirected credential request is allowed in the app.

Explicit native mode requires the actual listener and raw socket peer to be numeric loopback and Origin absent. Browser mode requires exact configured HTTPS Origin and raw trusted proxy CIDR. proxy_only_backend enforces the proxy CIDR on every app-control request even when native mode is also configured. Forwarded headers are never authority. Operators must enforce actual TLS/proxy deployment isolation; no public multiwriter service guarantee is made.

## Requests and responses

All POST bodies are application/json with unambiguous Content-Length, at most 16384 bytes. Duplicate sensitive headers, transfer encodings, duplicate JSON keys, nonfinite numbers, extra fields and unbounded identifiers refuse. GET query keys cannot repeat. All responses/errors have Cache-Control:no-store and Referrer-Policy:no-referrer.

POST /enroll: {command_id,project,password,replace_session_id?}. Returns201 {ok:true,control_session_id,control_token,runtime_id,expires_at}. Exact committed retry returns409 {ok:false,error:{code:CREDENTIAL_DELIVERY_UNKNOWN}}; never a replacement secret. Retry a failed publication with the same command to reconcile. A fresh explicit password step-up/new command may name an exact-owned same-account-session replacement. Password is checked live on every enrollment retry and is not included in persistent low-entropy command hashes.

POST /bindings: {command_id,title?,local_history_alias?}. Both labels are bounded display metadata. Returns200 {ok:true,receipt:{command_id,action,result_code,entity_id,entity_revision,selection_epoch}}. entity_id is the new binding ID. All command receipts use this fixed shape; optional numeric values are null when irrelevant. A binding's canonical host_conversation_id is server-issued app-session:<binding ID>. A caller chat ID cannot claim an existing host conversation.

GET /bindings?after_position=0&limit=50: {ok:true,items:[{binding_id,host_conversation_id,project,title,local_history_alias,revision,expires_at,revoked}],next_position}. Page limit is capped by host configuration; default is min(50,cap). next_position is null at exhaustion. Records are scoped to account/runtime/exact project grant.

GET /selection: {ok:true,selection:{selection_id,epoch,binding_id,binding_revision}|null}. null means the new control session has epoch0. A cleared selection has its incremented epoch and null binding fields. Use this read to reconcile current selection; exact command retries preserve their original payload.

POST /select: {command_id,binding_id,expected_binding_revision,expected_epoch}. Initial epoch is0. Receipt.entity_id is the selection ID; selection_epoch increments. POST /clear: {command_id,expected_epoch}. It increments the epoch and clears the selected binding. POST /revoke: {command_id,binding_id,expected_revision}. It revokes/increments the binding and fences every matching selection. Clearing/revoking a root is distinct from canceling independently granted child work.

GET /recovery?binding_id=<exact ID>&after_position=0&limit=32: {ok:true,binding:<above shape>,items:[bounded RecoveryItem],next_position,execution_available:false}. It uses the existing continuation service's read-only projection; no attachment or cleanup proof is inferred. RecoveryItem includes continuation_id,parent_session_id,epoch,owner_state,expires_at,authority_state,attachment_state,verification_phase,verification_code,pending_identity,pending_approval. Missing original projection codec yields honest unavailable verification metadata, never a clean default. Selection or chat labels cannot provide attachment authority. Unsupported managed execution is explicitly left for the next slice.

Errors are {ok:false,error:{code}} with no raw exception strings. 400 INVALID_APP_CONTROL_REQUEST/HEADERS/ROUTE/QUERY;401 APP_CONTROL_AUTH_REQUIRED;403 APP_CONTROL_REFUSED or APP_CONTROL_TRANSPORT_REFUSED;404 APP_BINDING_NOT_FOUND/APP_CONTROL_ROUTE_NOT_FOUND;409 APP_CONTROL_CONFLICT/APP_CONTROL_GRANT_CHANGED/CREDENTIAL_DELIVERY_UNKNOWN;429 APP_CONTROL_BUSY/APP_CONTROL_CAPACITY;503 APP_CONTROL_UNAVAILABLE/APP_CONTROL_OUTCOME_UNKNOWN/APP_RECOVERY_UNAVAILABLE. Failure after a database commit does not prove that no mutation happened. Retain exact command identity for reconciliation.

## Composition and limits

AppControlBinding(config_provider, *, account_open, account_path, fleet_path, private_inventory, lanes_provider=None, clock=time.time); start() initializes only when explicitly enabled and actual account signing secret is strong. Fixed source identity, configured all-root/private-store disjointness, current account/key/role/catalog/revision/expiry are checked for every operation and before publication. perform(action,payload,*,account_token,control_token,publish) publishes within the same-process exact account database admission guard; do not expose it as a model tool.

At most8 admitted requests/process,2 per raw peer,30 requests per peer/minute; bounded1024 peer rate rows, no unbounded inactive map. Password checks max2/process and8 per exact database/account/minute, bounded512 retained rate rows. Account mutation decorators coordinate register/login/revoke/set_account on the actual canonical database file identity. They do not claim cross-database atomicity or coordinate external SQL writers. Sessions/bindings/commands retain the reviewed store's immutable ceilings and finite no-eviction quotas.
