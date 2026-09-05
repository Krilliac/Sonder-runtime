# Managed worker ownership dependency

Ordinary runtimes still receive native `Thread` and `ThreadPoolExecutor` objects with native configuration. A required-new child can install one private worker owner before any observed factory use. The application layer uses `application.ports.runtime_threads`; bootstrap binds that port and the platform factory to the same owner. A late, duplicate or partial installation refuses admission and stops the proposed owner. There is no reset, global threading monkeypatch or discovery of threads created outside these factories.

Managed mode retains actual direct Thread objects and captures executor workers through the public `initializer` hook. Task accounting starts before native submission; cancelled submissions release their exact reservation without running the callable. Native start/submission occurs outside the owner mutex. Delayed starts and queued callables recheck stopped admission before invoking user work. Failed cleanup prevents further admissions and suppresses successful pool task results.

Each task and direct-thread exit performs current-thread cleanup. The concrete SQLite cleanup adapter closes and clears embedding, composition and generic SQLite-factory caches on that same thread, then closes remaining registered SQLite handles and checks that thread's ownership. Reused pool workers therefore reopen valid caches on their next task. An abandoned transaction or failed native close remains unresolved; another thread does not impersonate its owner.

Bounds are 128 reserved worker/direct-thread slots, 16 live pools and 256 submitted tasks by default. Managed pools use an explicit default of 32 worker slots, or an explicit size from 1 through 32. These are configured admission limits, not measured free resources. Successfully shut down pools release their reservations only after actual observed worker and shutdown-helper handles have exited. There is at most one retained shutdown helper per pool, in addition to the worker limit.

Owner close uses one monotonic deadline, from 0 through 30 seconds. Each pool's retained helper calls the public `shutdown(wait=True, cancel_futures=True)` API. A helper completing plus every observed worker handle exiting supplies the shutdown evidence; no private executor fields or runtime-shape assumptions are used. Blocked initializers, active tasks and native starts remain visible after timeout. Python threads are not terminated. The enclosing process owner must retain uncertainty until cooperative cleanup or its independently verified OS containment exit. A later thread snapshot may improve after actual exit; it does not rewrite an immutable higher-level owner receipt.

## Coverage boundary

The explicit package sweep covers 29 constructor calls in 22 `sonder_runtime` modules. Its AST regression is a source inventory, not a census of all library/native workers. Ten legacy constructors in root `server.py` and standard-library HTTP request-thread construction still require their coordinated integration hooks. Third-party worker owners, such as the PostgreSQL driver/pool, retain their separate authentic shutdown proof. This dependency alone does not certify complete Application readiness or clean shutdown, enable installed-service ownership, adopt old writers, or provide two-host fencing/HA.

## Evidence

Real tests cover worker reuse, task exceptions, cleanup failure, cancelled work, bounded capacity/reuse, blocked initialization and native start/submission races. Twenty tasks on one reused worker open all three actual SQLite caches under a four-handle budget and leave zero handles after each task. Subprocess tests bind both factory surfaces and reject prior native factory use. Unmanaged factories return exact native types.

The affected consumer run passed 219 tests with one NPU test skipped because the isolated environment lacks `psutil`. The final ownership and architecture run passed 43 tests. No installed runtime, service, account, firewall, remote node or live PostgreSQL instance was changed.
