"""Scratch benchmark (deleted before commit): effect-journal write-path overhead."""
import importlib.util, statistics, subprocess, sys, tempfile, time, types
from contextlib import contextmanager
from pathlib import Path

from sonder_runtime.adapters.persistence import owned_sqlite
from sonder_runtime.adapters.persistence.sqlite import effect_journal as branch_mod
from sonder_runtime.adapters.persistence.sqlite import effect_journal_chain as chain
from sonder_runtime.application.execution.effect_journal import EffectIntent, EffectOutcome, EffectState

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000
BASE = sys.argv[2] if len(sys.argv) > 2 else "HEAD"
src = subprocess.run(["git", "show", f"{BASE}:sonder_runtime/adapters/persistence/sqlite/effect_journal.py"],
                     capture_output=True, text=True, check=True).stdout
assert "effect_journal_chain" not in src, "baseline already has the chain"
main_mod = types.ModuleType("main_effect_journal")
exec(compile(src, "main_effect_journal.py", "exec"), main_mod.__dict__)


def fast_transaction(*a, **k):
    @contextmanager
    def cm():
        with owned_sqlite.transaction(*a, **k) as c:
            c.execute("PRAGMA synchronous=OFF")
            yield c
    return cm()


def run(mod, path, response=None, fast=False, **kw):
    if fast:
        mod.owned_sqlite_transaction = fast_transaction
    else:
        mod.owned_sqlite_transaction = owned_sqlite.transaction
    j = mod.SQLiteEffectJournal(path, **kw)
    t = time.perf_counter()
    for n in range(N):
        j.begin(EffectIntent(f"i-{n}", "run", "w", "op", "/ws", 1, f"k-{n}", f"{n:064x}"))
        extra = {} if response is None else {"response": response}
        j.outcome(EffectOutcome(f"i-{n}", EffectState.COMPLETED, "d" * 64, f"r-{n}",
                                worker_id="w", owner_epoch=1, **extra))
    return (time.perf_counter() - t) / N * 1e6, j


# instrument chain CPU inside the transaction
acc = {"append_row": 0.0, "record_response": 0.0, "encode_response": 0.0}
for name in acc:
    orig = getattr(chain, name)
    def wrap(*a, _o=orig, _n=name, **k):
        t = time.perf_counter()
        try:
            return _o(*a, **k)
        finally:
            acc[_n] += time.perf_counter() - t
    setattr(chain, name, wrap)

resp = {"success": True, "output": "x" * 512, "error_code": "", "error": ""}
configs = [
    ("main", main_mod, None, {}),
    ("branch", branch_mod, None, {}),
    ("branch+digest", branch_mod, resp, {}),
    ("branch+content", branch_mod, resp, {"record_response_content": True}),
]
for fast, rounds in ((True, 3), (False, 1)):
    label = "sync=OFF (CPU)" if fast else "durable (default sync)"
    res = {c[0]: [] for c in configs}
    for r in range(rounds):
        for name, mod, response, kw in configs:
            with tempfile.TemporaryDirectory() as d:
                us, j = run(mod, Path(d) / "e.db", response, fast, **kw)
                res[name].append(us)
                if name == "branch+content" and r == 0 and fast:
                    t = time.perf_counter(); v = j.verify_chain(); vt = time.perf_counter() - t
                    t = time.perf_counter(); rp = j.tool_response_replay("run"); rt = time.perf_counter() - t
                    print(f"verify_chain over {v.records_checked} records: {vt*1000:.0f} ms ok={v.ok}; "
                          f"replay build {len(rp)} entries: {rt*1000:.0f} ms complete={rp.complete}")
                del j
    print(f"== {label}, N={N}, rounds={rounds}")
    for name in res:
        print(f"  {name:15s} median {statistics.median(res[name]):9.1f} us/row  all={[round(x,1) for x in res[name]]}")

print("instrumented chain CPU (all runs):", {k: round(v, 3) for k, v in acc.items()}, "s")
calls_rows = N * 2 * (3 * 3 + 3)  # not used; see per-config below

# pure chain function cost per row, isolated
for name in acc: acc[name] = 0.0
with tempfile.TemporaryDirectory() as d:
    branch_mod.owned_sqlite_transaction = fast_transaction
    us, j = run(branch_mod, Path(d) / "e.db", resp, True)
print(f"isolated (branch+digest, sync=OFF): total {us:.1f} us/row; chain parts per row: "
      + ", ".join(f"{k}={v/N*1e6:.1f}us" for k, v in acc.items()))

# migration of a 10k-row legacy journal, and 1 MB response encode
with tempfile.TemporaryDirectory() as d:
    main_mod.owned_sqlite_transaction = fast_transaction
    run(main_mod, Path(d) / "e.db", None, True)
    branch_mod.owned_sqlite_transaction = owned_sqlite.transaction
    t = time.perf_counter(); j = branch_mod.SQLiteEffectJournal(Path(d) / "e.db"); mt = time.perf_counter() - t
    t = time.perf_counter(); branch_mod.SQLiteEffectJournal(Path(d) / "e.db"); rt = time.perf_counter() - t
    print(f"first open of {N}-row pre-chain journal (anchor): {mt*1000:.0f} ms; reopen: {rt*1000:.1f} ms; "
          f"verify ok={j.verify_chain().ok} legacy={j.verify_chain().legacy_rows}")
big = {"success": True, "output": "y" * (1 << 20), "error_code": "", "error": ""}
t = time.perf_counter()
for _ in range(20):
    chain.encode_response.__wrapped__ if False else None
    enc = chain.encode_response(big, keep_content=False, max_bytes=256 * 1024)
print(f"encode_response 1 MiB output: {(time.perf_counter()-t)/20*1000:.2f} ms/call")
