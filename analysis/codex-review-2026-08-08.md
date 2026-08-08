# Adversarial review of six Sonder Runtime modules

Date: 2026-08-08  
Scope: current working-tree versions of `code_improve.py`, `tier_router.py`, `consult.py`, `command_router.py`, `project_scaffold.py`, and `environment_probe.py`  
Mode: report only; no existing file was modified

## Summary

| Rank | Severity | ID | Module | Finding |
|---:|:---:|:---|:---|:---|
| 1 | High | CI-1 | `code_improve.py` | A reply containing a second top-level definition is accepted as a one-function improvement and can be written to disk. |
| 2 | High | CI-2 | `code_improve.py` | A syntactically invalid candidate can receive `ok=True`; neither the splice nor the guard parses it. |
| 3 | High | CI-3 | `code_improve.py` | Parentheses inside a valid signature string/comment fool the lexical depth counter and can make a splice delete later top-level definitions. |
| 4 | High | CR-1 | `command_router.py` | Prefix-only lifecycle rules turn ordinary coding prose into `/new` or `/exit`, discarding context or leaving the REPL. |
| 5 | High | CR-2 | `command_router.py` | Conditional or explanatory control-language is executed unconditionally; `/qualityfix apply` and `/agentcancel` are concrete examples. |
| 6 | High | CO-1 | `consult.py` | Empty model replies are counted as successful answers instead of failures. |
| 7 | High | CO-2 | `consult.py` | Lowercase/mixed-case `error:` replies are counted as answers and can yield a high-confidence agreement verdict. |
| 8 | High | PS-1 | `project_scaffold.py` | The filename-safe name filter is not language-identifier-safe; accepted names generate uncompilable C# and Python test sources. |
| 9 | High | EP-1 | `environment_probe.py` | `shutil.which` proves only PATH discoverability, not installation or executability, contrary to the module contract used in every agent prompt. |
| 10 | Medium | CI-4 | `code_improve.py` | Decorators are excluded from extraction and reply decorators are silently discarded. |
| 11 | Medium | CI-5 | `code_improve.py` | A standalone Markdown-fence-looking line inside a Python string is deleted from the candidate. |
| 12 | Medium | CI-6 | `code_improve.py` | `#` inside string literals fools the comment stripper and causes real edits to be rejected as comment-only. |
| 13 | Medium | CI-7 | `code_improve.py` | Valid Unicode identifiers containing combining marks cannot be recognized in replies. |
| 14 | Medium | TR-1 | `tier_router.py` | One-shot or empty availability iterables can produce a tier that is not present while the reason says it is being used. |
| 15 | Medium | CO-3 | `consult.py` | The fallback tokenizer drops all non-ASCII words, so identical non-Latin answers are reported as disagreement. |
| 16 | Medium | CO-4 | `consult.py` | The judge parser accepts contradictory/multiline output solely from its first token and can assign high confidence. |
| 17 | Medium | CO-5 | `consult.py` | Any exception while checking cloud policy silently disables the cloud tier with no downgrade/report. |
| 18 | Medium | CR-3 | `command_router.py` | File-command patterns consume only a prefix and silently discard the requested follow-up work or a later negation. |
| 19 | Medium | PS-2 | `project_scaffold.py` | `with_tests=True` emits test files without wiring fresh scaffolds to build/run those tests. |
| 20 | Medium | PS-3 | `project_scaffold.py` | An unvalidated injected GUID can make the generated XML and solution invalid. |
| 21 | Medium | PS-4 | `project_scaffold.py` | Project-name sanitization silently removes characters and creates collisions; Windows device names are also accepted. |
| 22 | Low | CI-8 | `code_improve.py` | A CRLF module is returned with mixed CRLF/LF endings after a splice. |

There are 9 high, 12 medium, and 1 low findings. No critical finding was assigned because command-router deletion is dry-run and the dangerous code-improvement path still requires either an explicit apply flag or REPL confirmation. The high code-improvement findings nevertheless cross the module's stated safety boundary and can reach the overwrite calls in `server.py:14460-14471` and `sonder_repl.py:687-701`.

## Verification method and boundary

I read every line of all six requested modules and their focused tests. For findings that cross a module boundary, I also traced the immediate caller/dispatcher. Each result below is derived by stepping the exact input through the cited branches and stating the resulting value or side effect.

The checkout's `venv\\Scripts\\python.exe` points to a removed `Python312\\python.exe`, so the focused pytest suite was not runnable from this shell. A bounded in-memory Python execution request through the live runtime was cancelled and was not retried. Thus “actual” below means deterministic source-level path tracing against the live working tree, not a claimed test execution. This limitation does not affect the regex/counting outcomes, but it is recorded so the evidence boundary is explicit.

## `code_improve.py`

### CI-1 — High — a multi-definition reply is accepted as “one function”

**Lines:** `92-104`, `261-275`.

**Breaking input:**

```python
source = '''def f(x):
    return x


def g():
    return 2
'''

reply = '''def f(x):
    if x is None:
        x = 0
    return x


def planted():
    pass
'''
```

Call `improve_function(source, "f", ask_fn_returning(reply))`.

**Expected:** reject the reply because the documented contract says the reply must be a single function and the module “splices only that function” (`1-15`, `84-90`, `213-224`).

**Actual trace:**

1. `splice_function` finds only the first `def` at line 93.
2. Line 97 sets `body = text[match.start():]...`, which retains everything through `def planted`.
3. `_block_bounds(original, "f")` ends at `def g`, so line 104 inserts both `f` and `planted` before `g`.
4. Line 261 re-extracts only `f` because `def planted` is its next column-zero boundary.
5. The unchanged `return x` avoids the return-rewrite objection; the added `None` guard trips no other objection and the candidate is larger, not smaller.
6. Lines 273-275 therefore set `ok=True`, with `edited` containing `planted`.

This is directly reachable by the overwrite callers cited in the summary.

### CI-2 — High — invalid Python can receive `ok=True`

**Lines:** `84-104`, `253-275`.

**Breaking input:**

```python
source = '''def f(x):
    return x
'''

reply = '''def f(x):
    if x is None
        x = 0
    return x
'''
```

**Expected:** a “usable, guarded candidate” (`217`) must parse; this reply should be rejected with a syntax reason.

**Actual trace:** the def regex matches, the original block exists, the candidate is larger than the shrink floor, and the only code additions are `if x is None` and `x = 0`. The original `return x` is unchanged, so none of `diff_objection`'s rules (`156-184`) fires. No `ast.parse` or `compile` occurs anywhere. Lines 273-275 return `ok=True` for a module that fails Python parsing at the missing colon.

### CI-3 — High — parentheses in strings/comments can widen a block to EOF

**Lines:** `40-66`, especially `55-60`.

**Breaking input:**

```python
source = '''def f(x="("):
    """''' + ('a' * 200) + '''"""
    return x


def g():
    pass
'''

reply = '''def f(x="("):
    """''' + ('a' * 200) + '''"""
    if x is None:
        x = ""
    return x
'''
```

**Expected:** the documented signature balancer (`41-46`) should locate the closing `)` of this valid signature and replace only `f`.

**Actual trace:** line 57 counts characters rather than Python tokens. On `def f(x="("):` it counts two opens (the real `(` and the string's `(`) and one close, leaving `depth == 1`. Every later normal line has net zero parentheses, including `def g():`, so the loop reaches EOF. `_block_bounds` returns the whole remainder of the module as `f`; splicing the reply deletes `g`. The long docstring keeps the edited text above the 75% shrink floor, the unchanged return avoids the contract guard, and removing only `def g():`/`pass` triggers no objection. This shape can therefore also reach `ok=True`.

The same defect is triggered by an unmatched parenthesis character in a signature comment.

### CI-4 — Medium — decorators are outside the “full function” and reply decorators vanish

**Lines:** `41-50`, `69-76`, `92-104`.

**Breaking input:**

```python
source = '''@audit
def f(x):
    return x
'''

reply = '''@cache
def f(x):
    if x is None:
        x = 0
    return x
'''
```

**Expected:** either treat decorators as part of the full function and replace them, or reject a reply that changes material outside the allowed def block.

**Actual trace:** `_block_bounds` starts at the `def`, so `extract_function` omits `@audit`. In the reply, line 93 finds the `def`, and line 97 slices from that match, silently dropping `@cache`. The output retains the original `@audit` from `lines[:first]` and accepts the body change. The caller is not told that part of the model reply was discarded.

Normal nested definitions do survive the block scan because they are indented, and an unchanged existing decorator remains attached. I found no separate nested-def deletion bug beyond CI-3's broken signature depth.

### CI-5 — Medium — fence stripping edits string data inside the function

**Lines:** `31`, `79-81`.

**Breaking input:**

```python
reply = '''def f():
    return """alpha
```
omega"""
'''
```

**Expected:** `strip_fences` should remove only a Markdown wrapper around the reply, not code/data inside the function.

**Actual trace:** `_FENCE_RE.sub("", ...)` is global and multiline. The literal-content line consisting of ``` matches the same regex as a wrapper and is deleted. The function's returned string changes from `"alpha\n```\nomega"` to `"alpha\n\nomega"` without a rejection or report.

### CI-6 — Medium — `#` in strings causes a false comment-only rejection

**Lines:** `114-126`, `137-142`.

**Breaking input:** original `return "#old"`; candidate `return "#new"`.

**Expected:** this is a real string-literal change and should proceed to the substantive guards.

**Actual trace:** line 123 uses `line.split("#", 1)` without tokenizing Python. Both lines reduce to the same prefix, `return "`, so `before == after` and line 142 returns `comment-only change`. The failure is reported, but the reason is false and a real change is silently erased from comparison.

### CI-7 — Medium — valid combining-mark identifiers fail reply recognition

**Lines:** `28`, `34-37`, `93-95`.

**Breaking input:** a valid Python identifier spelled `a\u0301` (letter `a` plus COMBINING ACUTE ACCENT):

```python
source = "def a\u0301():\n    return 1\n"
reply = "def a\u0301():\n    return 2\n"
```

**Expected:** a valid top-level Python function can be listed and replaced.

**Actual trace:** Python's identifier grammar permits the combining mark as an identifier continuation, but `\w+` in these regular expressions does not consume that mark. `_DEF_RE` cannot list it and the reply search cannot reach the following `(`, so `splice_function` returns `None`. Direct `_block_bounds` with the exact supplied name can find the source, making `improve_function` fail later with the misleading generic reason “reply was not a single replaceable function.” Ordinary ASCII, accented precomposed letters, and CJK letters do not share this specific failure.

### CI-8 — Low — CRLF input becomes mixed-line-ending output

**Lines:** `71`, `92`, `97`, `99`, `104`.

**Breaking input:** a CRLF-only source and CRLF-only reply for a normal function.

**Expected:** a textual splice should preserve the module's newline convention.

**Actual trace:** `splitlines(keepends=True)` preserves CRLF in untouched lines, but `strip()` removes the reply's terminal CRLF, line 97 appends a bare `\n`, and line 104 appends another bare `\n`. The replaced region is LF-terminated while neighboring original lines remain CRLF. Python still parses it, so this is low severity rather than a parser failure.

## `tier_router.py`

### TR-1 — Medium — availability fallback can return an unavailable tier

**Lines:** `92-112`.

**Breaking input A:**

```python
route("what is the exact RFC wording", iter(["reasoning"]))
```

**Expected:** because `reasoning` is the only present tier, fallback should return it and truthfully report that choice.

**Actual trace:** the preferred `cloud-general` membership check at line 102 consumes the one-shot iterator through exhaustion. Every membership check in lines 104-107 then sees an exhausted iterator. Line 109 evaluates another empty iterator and returns the default `code`. The result says `fallback_used=True` and “using code” even though `code` was never available.

**Breaking input B:** `route("what is the exact RFC wording", [])` follows the same `else` and returns `code`, again contradicting the docstring's “falls back to a present one” promise (`95-97`).

The production callers currently pass `set(TIERS)`, so they avoid iterator exhaustion. Multiline prompts are handled by `search`, and the source explicitly documents that pasted material dominates even external-recall cues (`60-64`); I did not count that known heuristic limitation as a new finding.

## `consult.py`

### CO-1 — High — empty answers are successes

**Lines:** `26-37`, `122-149`.

**Breaking input:** `consult("q", ["code", "reasoning"], ask_fn=lambda *_: "")`.

**Expected:** zero usable answers means `agree=None`, `confidence="unknown"`, and a note naming failures.

**Actual trace:** `_ask` converts the result to `""`; `_failure("")` is false because neither check at line 29 matches. Both empties enter `ok`, so the fewer-than-two guard is skipped. The empty judge is also treated as non-failed but malformed; the overlap fallback sees empty token sets and returns disagreement. Final result: `agree=False`, `confidence="unknown"`, with “Judge failed” rather than “no answers.”

### CO-2 — High — lowercase error replies can become high-confidence agreement

**Lines:** `26-37`, `122-153`.

**Breaking input:** an injected `ask_fn` that returns `"error: timeout"` for each answer request and `"YES. Same conclusion."` for the judge request.

**Expected:** the docstring says returned model errors are failures (`108`); the result should have fewer than two successes and no verdict.

**Actual trace:** `_failure` lowercases only for the “no model...” substring. `value.startswith("ERROR:")` remains case-sensitive, so both lowercase errors enter `ok`. The judge starts with YES, `_JUDGE_RE` accepts it, and lines 151-153 return `agree=True`, `confidence="high"`, “All tiers agree on the substance.” This is a false high-confidence verdict over two backend failures.

### CO-3 — Medium — fallback overlap is ASCII-only

**Lines:** `13`, `53-60`.

**Breaking input:** two identical answers `"使用缓存"`, followed by a failed or malformed judge.

**Expected:** normalized-token Jaccard overlap of identical non-empty answers is 1.0, so the conservative fallback should tentatively agree.

**Actual trace:** `[a-z0-9]+` extracts no tokens from either answer. Line 56 treats both sets as unusable and returns false, so the consult reports heuristic disagreement. The module-level promise says “normalized-token Jaccard overlap” without disclosing that all non-ASCII text is discarded.

### CO-4 — Medium — contradictory judge output is accepted from the prefix only

**Lines:** `14`, `141-153`.

**Breaking input:** judge reply `"YES. They agree.\nNO. Actually the recommendations conflict."`.

**Expected:** the judge prompt requires YES or NO and one sentence. A contradictory, two-verdict response is malformed and should fall back with unknown confidence.

**Actual trace:** `_JUDGE_RE.match` reads only the initial YES and performs no full-response validation. Lines 151-153 return `agree=True`, `confidence="high"`. The opposite order produces a low-confidence disagreement; in both cases trailing contradiction is ignored.

### CO-5 — Medium — cloud-policy errors are silently converted to “cloud disabled”

**Lines:** `76-94`, especially `85-91`.

**Breaking input:** call `default_tiers()` while `server.cloud_allowed()` raises, for example `RuntimeError("policy database unavailable")`.

**Expected:** inability to read policy should be distinguishable from an explicit disabled result, at least in the consult note.

**Actual trace:** the broad `except Exception` sets `cloud_ok=False` and returns `['code', 'reasoning']`. No error or downgrade reaches `consult` or its caller. This violates the “cloud model joins when cloud is enabled” contract in precisely the case where enabled state cannot be checked.

Beyond these cases, duplicate tiers are correctly collapsed before asking, every pair is checked in the N-answer heuristic, and a failed third answer is explicitly surfaced while two successful answers can still be judged. I found no additional real issue in those paths.

## `command_router.py`

### CR-1 — High — lifecycle prefixes hijack ordinary work

**Lines:** `11-19`, `67-80`, `191-215`; dispatch at `sonder_repl.py:723-738`, `1062-1067`, `1103-1104`.

Most rules are start-anchored but not end-anchored. `\b` after a short phrase is not enough to establish that the whole turn is a command.

| Exact input | Expected | Actual resolution and dispatch |
|:---|:---|:---|
| `reset the session token when it expires` | Normal coding/security question | `/new`; the REPL replaces `session_id` and clears last-response state. |
| `start over with the explanation, but keep this chat context` | Rewrite the explanation while preserving context | `/new`; the explicit “keep context” clause is discarded. |
| `quit stalling and answer the question` | Answer the question | `/exit`; the REPL loop breaks. |

This directly contradicts the design claim that a plain coding question/task falls through (`13-16`). CRLF/multiline input makes the negation problem worse because line 198 collapses every whitespace run to one space before matching.

### CR-2 — High — conditional controls execute unconditionally

**Lines:** `107-128`, `198-214`; dispatch at `sonder_repl.py:840-855`.

| Exact input | Expected | Actual |
|:---|:---|:---|
| `fix the memory quality, but only show me what would change` | Dry-run/report before applying | `/qualityfix apply`; dispatch calls `memory_quality_repair(apply=True)`. |
| `cancel all agents only if the tests fail` | Preserve agents unless the condition becomes true | `/agentcancel`; the condition is discarded and cancellation is dispatched immediately. |
| `show agents how to reproduce the bug` | Explanatory request addressed to agents | `/agents`; only status is shown. |
| `help me debug this crash` | Debugging help | `/help`; the crash request is discarded. |

These inputs open with documented trigger words, but they are not standalone command asks. The implementation has no end anchor, negation/conditional guard, or confirmation for the synthesized state-changing commands.

### CR-3 — Medium — file rules discard follow-up intent and later negation

**Lines:** `150-158`, `198-214`; delete dispatch at `sonder_repl.py:995-996`.

**Breaking inputs:**

- `read the file foo.py and explain why it fails` resolves to `/read foo.py`; the explanation request is discarded because the regex captures only the first non-space path and is not end-anchored.
- `delete the file scratch.txt\r\nActually, do not delete it` is normalized to one line, then resolves to `/delete scratch.txt`; the later negation is ignored. The current REPL dispatch makes this a dry-run, so it does not immediately delete the file, but it still performs an operation the user expressly negated and emits a confirmation string for deletion.

The exact-path form `read foo.py` is correctly end-anchored, already-slash inputs correctly return `None`, and the scaffold rule correctly uses an end anchor. Those paths survived the adversarial check.

## `project_scaffold.py`

### PS-1 — High — accepted names are not safe source-language identifiers

**Lines:** `47-52`, `175-197`, `225-239`, `435-464`.

**Breaking inputs and outputs:**

| Call | Accepted output | Expected vs. actual |
|:---|:---|:---|
| `render("csharp", "my-app")` | `namespace my-app;` | Expected a compilable C# skeleton; `-` is not valid in an unescaped namespace identifier. |
| `render("csharp", "123app")` | `namespace 123app;` | A filename may start with a digit, but a C# identifier may not. |
| `render("python", "class", with_tests=True)` | `from class.__main__ import main` | `identifier()` calls this “safe,” but a Python keyword creates a SyntaxError in the emitted test. |

The root problem is that `base` permits `[A-Za-z0-9_.-]` and `identifier` handles characters/leading digits but not target-language keywords. `@NAME@` is then inserted directly into language syntax. This is a contract lie: the function returns without error, but the project is not a usable skeleton.

### PS-2 — Medium — opt-in tests are emitted but not wired into fresh projects

**Lines:** `233-303`, `416-432`, `461-464`.

**Breaking input:** `render("typescript", "Demo", with_tests=True)`.

**Expected:** the server-facing contract says this “adds a unit-test skeleton”; a fresh scaffold's declared build/test workflow should be able to consume it.

**Actual trace:** `src/index.test.ts` imports `node:test` and `node:assert`, but `package.json` declares only `typescript` and has no `@types/node`; it also has no `test` script. A normal `npm install && npm run build` cannot resolve the Node built-in type declarations, and `npm test` has no script. The Node scaffold likewise adds `test/index.test.js` without a `test` script. The Python test depends on pytest's `capsys`, but the pyproject declares no test dependency. These are files named like tests, not a wired fresh-project test path.

### PS-3 — Medium — GUID injection is not validated or escaped

**Lines:** `24-25`, `75-99`, `102-147`, `435-459`.

**Breaking input:** `render("cpp-msvc", "Demo", guid="BAD&GUID")`.

**Expected:** the documented GUID injection seam should accept a GUID or reject invalid input, preserving well-formed `.vcxproj` XML and `.sln` GUID syntax.

**Actual trace:** line 455 merely uppercases and strips outer braces. Literal token substitution produces `<ProjectGuid>{BAD&GUID}</ProjectGuid>`, where the unescaped `&` makes the XML not well-formed; the same non-GUID appears in solution configuration keys. No validation or parse check reports the problem.

### PS-4 — Medium — lossy name filtering creates collisions and accepts Windows devices

**Lines:** `20-24`, `448-455`.

**Breaking inputs:**

- `render("python", "A/B")` and `render("python", "AB")` produce the same paths/content because line 448 deletes `/` instead of rejecting or replacing it. The module description says `@NAME@` is the project name “as given”; the caller is not told it became a different, colliding name.
- `render("cpp-msvc", "CON")` succeeds and returns `CON.sln`/`CON.vcxproj`. On Windows, `CON` remains a reserved device name even with an extension, so the later writer cannot create the promised scaffold. The server caller catches individual write exceptions and reports an incomplete scaffold, but rejecting this host-invalid name at render time would avoid partial creation.

All template tokens are replaced literally and ordinary safe names produce structurally consistent path keys. I found no additional nested-template/token parser issue.

## `environment_probe.py`

### EP-1 — High — PATH presence is presented as installed and runnable

**Lines:** module contract `1-15`; implementation `40-46`, `71-87`, `91-110`. The brief is injected into every agent run at `server.py:11965`.

**Breaking environment A:** the runtime is executing from a valid `sys.executable`, but `PATH` contains no `python`/`python3` launcher (for example, PATH points only to an empty directory).

**Expected:** because Python is demonstrably installed and currently running, the promised inventory of installed interpreters/run-code languages should report it as available.

**Actual trace:** `_which_map(_TOOLCHAINS)` has no fallback to `sys.executable`, so `toolchains` contains no Python entry even though the same dict reports `python_executable` and `python_version`. `agent_brief` can therefore say `tools: (none) | python 3.x`, a contradictory command-selection hint.

**Breaking environment B:** PATH contains a file named `python.exe` that passes `shutil.which`'s path/access check but is a broken launcher or non-executable payload.

**Expected:** the module's claim that it discovers “which run_code languages will actually work here” should not advertise it as working.

**Actual trace:** `_which_map` records the path without starting or validating it. The module explicitly never spawns subprocesses, so it cannot establish the stronger “actually work” property. Inaccessible PATH entries and broken shims are also indistinguishable from absent/healthy tools, and no warning list is returned.

The honest contract is “PATH-discoverable command names,” not “installed toolchains” or “languages that actually work.” Caching/refresh behavior, platform booleans, one-line formatting, and preferred-shell order are internally consistent with their docstrings; I found no additional real parser or silent-failure issue in this 135-line module.

## Suggested remediation order (not applied)

1. Parse candidate Python with `ast` and use AST/token boundaries for exact one-function replacement, including decorators; reject any extra top-level reply node.
2. Require command-like inputs to match the entire normalized turn for state-changing commands, and reject conditionals/negations rather than truncating them.
3. Validate scaffold names per target language, validate GUIDs, and make every `with_tests` scaffold expose a fresh-install test command.
4. Treat empty and case-insensitive error replies as consult failures; validate the entire judge verdict and use a Unicode tokenizer.
5. Materialize/validate tier availability once before fallback.
6. Rename environment-probe claims to PATH discovery or add bounded executable health checks and explicit probe warnings.
