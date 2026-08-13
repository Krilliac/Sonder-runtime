"""json_schema_verifier -- a stdlib-only oracle for "does this JSON match this
JSON Schema", with an explicit channel for the parts it could not check.

Fits the repo's verifier contract (see verifiers.py): fn(artifact, spec) ->
Verdict(passed, reason, detail). `artifact` is a JSON string; `spec={"schema":
{...}}` gives the schema to check it against. There is no external tool or model
involved, so this verifier never raises VerifierUnavailable -- malformed JSON or
a bad/missing schema is just a failed Verdict explaining why.

Two channels, not one
---------------------
A schema keyword is in exactly one of three states here, and the third is the
reason this module has two result channels instead of a bare error list:

* **violated** -- the data broke a keyword this module enforces. That is a hard
  failure. Nothing is repaired, coerced, defaulted or re-asked: rejecting
  non-conforming data is the entire point.
* **satisfied** -- the data met a keyword this module enforces.
* **unchecked** -- the keyword (or a whole subtree) is something this module
  cannot evaluate. It is *never* folded into "satisfied". `check()` returns it
  as a separate `unchecked` list, `coverage_gaps()` exposes it on its own, and
  `json_schema_verify()` fails closed on it: an unchecked keyword produces
  Verdict(passed=False) saying so, because a passing Verdict is a claim the
  data was checked.

`validate()` deliberately returns violations *only*, so a caller that just wants
"did this break the schema" is not handed "and here is what I could not see".
Such a caller must consult `coverage_gaps()` too, or it is back to reading
silence as a pass.

Enforced keywords (`CHECKED_KEYWORDS`)
--------------------------------------
    type (single name or a union list, plus the local extension "any"),
    enum, const,
    required, properties, patternProperties, additionalProperties,
    propertyNames, minProperties, maxProperties,
    items, minItems, maxItems, uniqueItems,
    minLength, maxLength, pattern,
    minimum, maximum, exclusiveMinimum, exclusiveMaximum, multipleOf,
    allOf, anyOf, oneOf, not,
    $ref -- resolved against this document only ("#" and "#/..." JSON pointers,
            which covers "#/$defs/..." and "#/definitions/...").

Annotation keywords (`ANNOTATION_KEYWORDS`) assert nothing and are not reported
as gaps. Every *other* keyword -- `if`/`then`/`else`, `contains`,
`dependentRequired`, `dependentSchemas`, `unevaluatedProperties`,
`prefixItems`, `format` (annotation-only in JSON Schema, but reported here
rather than silently ignored), and anything nobody has anticipated -- is
reported as unchecked. That is the complement of what is enforced rather than a
list somebody remembered to enumerate, so an unfamiliar keyword fails closed by
default.

Absences fail closed too, because they are what made this verifier dangerous:

* a `$ref` node carries no "type", so the old code defaulted the node to "any"
  and accepted literally any value -- a reply of "totally the wrong shape"
  verified clean against a `$ref` schema. Local refs are now followed; a ref
  this module cannot follow (external, or recursive with no data to consume) is
  unchecked, and one that does not resolve is an error.
* `required`/`properties` written without an explicit `"type": "object"` were
  skipped for the same reason. Keywords are now applied by the *instance* type,
  as JSON Schema specifies, so they apply to any object. A keyword that could
  not apply because the value is of another type is reported as unchecked.

Caveats worth knowing. Both are reported as unchecked rather than guessed at,
because a limitation of this module is not evidence about the data -- whatever
produced the data was constrained by the *whole* schema, so rejecting it for a
blind spot here would remove capability the caller legitimately has:

* `pattern`/`patternProperties` are compiled with Python's `re`, which is close
  to but not JSON Schema's ECMA-262 dialect. A pattern `re` cannot compile is a
  gap, not a failed match.
* `items` in the draft-07 per-position tuple form is not applied.
* nesting past `MAX_DEPTH` levels is not descended into. The traversal costs
  several Python frames per level, so an unbounded descent raised
  RecursionError out of a function documented as never raising.

`$ref` resolution is document-local, ignoring `$id` re-basing.
"""
import collections
import json
import re

Verdict = collections.namedtuple("Verdict", ["passed", "reason", "detail"])

# What `check()` returns: violations and coverage gaps, never merged.
CheckResult = collections.namedtuple("CheckResult", ["errors", "unchecked"])

_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    # bool is a subclass of int in Python -- exclude it from integer/number
    # so {"type": "integer"} doesn't silently accept `true`/`false`.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "any": lambda v: True,
}

# Keywords this module actually enforces. A downstream coverage report should
# derive its "not independently verified" set as the complement of THIS, so the
# two cannot drift: adding a keyword here without implementing it is caught by
# tests/test_json_schema_verifier.py, which requires every name here to reject
# some violating datum.
CHECKED_KEYWORDS = frozenset({
    "type", "enum", "const",
    "required", "properties", "patternProperties", "additionalProperties",
    "propertyNames", "minProperties", "maxProperties",
    "items", "minItems", "maxItems", "uniqueItems",
    "minLength", "maxLength", "pattern",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "allOf", "anyOf", "oneOf", "not",
    "$ref",
})

# Keywords that assert nothing about the data. Ignoring these is not a coverage
# gap -- there is nothing to check. `$defs`/`definitions` are subschema
# containers, reached through `$ref` rather than applied in place.
ANNOTATION_KEYWORDS = frozenset({
    "$anchor", "$comment", "$defs", "$id", "$schema", "definitions",
    "default", "deprecated", "description", "examples",
    "readOnly", "title", "writeOnly",
})

# Which keywords only make sense for which instance type. Used to report a
# keyword that could not apply, instead of quietly scoring it as satisfied.
_OBJECT_KEYWORDS = ("required", "properties", "patternProperties",
                    "additionalProperties", "propertyNames",
                    "minProperties", "maxProperties")
_ARRAY_KEYWORDS = ("items", "minItems", "maxItems", "uniqueItems")
_STRING_KEYWORDS = ("minLength", "maxLength", "pattern")
_NUMBER_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum",
                    "exclusiveMaximum", "multipleOf")

_MISSING = object()

# How deep the traversal will descend before reporting the rest as unchecked.
# This descent costs several Python frames per level, so without a bound a
# deeply nested document raised RecursionError straight out of
# `json_schema_verify` -- at 500 levels, while `json.loads` parsed the same text
# happily, which made this module the limiter rather than the input. Depth we
# cannot walk is ignorance like any other: reported, never raised, and never
# mistaken for a pass. Real schemas nest in the tens; this is far above that.
MAX_DEPTH = 100


class _Ctx(object):
    """Carries the two output channels plus what `$ref` needs: the document
    root to resolve against, and the refs currently being expanded (so a cycle
    that consumes no data terminates instead of recursing forever)."""

    __slots__ = ("errors", "unchecked", "root", "active_refs", "depth",
                 "max_unique_items")

    def __init__(self, root, active_refs=None, depth=0,
                 max_unique_items=None):
        self.errors = []
        self.unchecked = []
        self.root = root
        self.active_refs = set() if active_refs is None else set(active_refs)
        self.depth = depth
        self.max_unique_items = max_unique_items


def _show(value):
    """A short, JSON-ish rendering for error messages."""
    try:
        text = json.dumps(value)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _kind(value):
    """The JSON type name of a value, for messages."""
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPE_CHECKS[name](value):
            return name
    return type(value).__name__


def _json_equal(a, b):
    """JSON equality, which is not Python equality: `True == 1` in Python but
    `true` and `1` are different JSON values, and `enum`/`const`/`uniqueItems`
    all hinge on that distinction."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return (set(a) == set(b)
                and all(_json_equal(a[k], b[k]) for k in a))
    return False


def _resolve_pointer(root, ref):
    """Resolve a document-local JSON pointer ("#", "#/$defs/x", "#/a/0")."""
    if ref == "#":
        return root
    node = root
    for raw in ref[2:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return _MISSING
            node = node[token]
        elif isinstance(node, list):
            try:
                index = int(token)
            except (TypeError, ValueError):
                return _MISSING
            if index < 0 or index >= len(node):
                return _MISSING
            node = node[index]
        else:
            return _MISSING
    return node


def _malformed(ctx, path, keyword, message):
    """A keyword whose own value is unusable: an error (the schema is broken)
    and a gap (it therefore checked nothing)."""
    ctx.errors.append('%s: "%s" %s' % (path, keyword, message))
    ctx.unchecked.append((path, '"%s" %s, so it checked nothing' % (keyword, message)))


def _uncheckable(ctx, path, keyword, message):
    """A keyword this module cannot evaluate here even though the schema may be
    perfectly legal elsewhere. A gap, never a violation: rejecting the data for
    a limitation of this checker would remove capability the caller has, since
    whatever produced the data was constrained by the full schema."""
    ctx.unchecked.append((path, '"%s" %s, so it was not checked' % (keyword, message)))


def _count_bound(ctx, path, schema, keyword):
    """Read an integer-valued keyword (min/maxItems, min/maxLength,
    min/maxProperties), or say why it could not be used. Returning None on a
    bad value without a word would be a guard that silently no-ops -- exactly
    the shape of the bug this module is being fixed for."""
    if keyword not in schema:
        return None
    limit = schema[keyword]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        _malformed(ctx, path, keyword,
                   "must be a non-negative integer, got %s" % _show(limit))
        return None
    return limit


def _compiled(ctx, path, keyword, raw):
    """Compile a schema regex, or record why it could not be checked. JSON
    Schema's dialect is ECMA-262; Python's `re` is close but not identical, so
    a pattern that fails to compile HERE is this module's blind spot, not
    evidence about the data."""
    try:
        return re.compile(raw)
    except (re.error, TypeError) as exc:
        _uncheckable(ctx, path, keyword,
                     "%s is not a regular expression Python can compile (%s)"
                     % (_show(raw), exc))
        return None


def _validate_ref(value, ref, path, ctx):
    if not isinstance(ref, str):
        ctx.errors.append('%s: "$ref" must be a string, got %s' % (path, _show(ref)))
        return
    if ref != "#" and not ref.startswith("#/"):
        # An external or $id-relative reference. Nothing here can fetch it, and
        # pretending the subtree passed is the failure this module exists to
        # stop, so it is a coverage gap.
        ctx.unchecked.append(
            (path, '"$ref" %s is not a pointer into this document, so it was '
                   "not followed and nothing below it was checked" % ref))
        return
    # Keyed on the value as well as the pointer: a self-referential schema over
    # finite data descends into new values each time and must keep validating;
    # only a ref that returns to the same node with the same value is a cycle.
    key = (ref, path, id(value))
    if key in ctx.active_refs:
        ctx.unchecked.append(
            (path, '"$ref" %s is recursive with no data to consume, so it was '
                   "not followed again" % ref))
        return
    target = _resolve_pointer(ctx.root, ref)
    if target is _MISSING:
        ctx.errors.append('%s: "$ref" %s does not resolve in this schema' % (path, ref))
        ctx.unchecked.append(
            (path, '"$ref" %s does not resolve, so whatever it pointed at went '
                   "unchecked" % ref))
        return
    ctx.active_refs.add(key)
    try:
        _validate(value, target, path, ctx)
    finally:
        ctx.active_refs.discard(key)


def _subcheck(value, subschema, path, ctx):
    """Validate against a subschema in isolation, so a combinator can inspect
    the outcome rather than leaking a branch's errors into the parent."""
    sub = _Ctx(ctx.root, ctx.active_refs, ctx.depth, ctx.max_unique_items)
    _validate(value, subschema, path, sub)
    return sub


def _validate_combinators(value, schema, path, ctx):
    if "allOf" in schema:
        branches = schema["allOf"]
        if not isinstance(branches, list):
            ctx.errors.append('%s: "allOf" must be an array, got %s' % (path, _show(branches)))
        else:
            for branch in branches:
                sub = _subcheck(value, branch, path, ctx)
                ctx.errors.extend(sub.errors)
                ctx.unchecked.extend(sub.unchecked)

    for keyword in ("anyOf", "oneOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        if not isinstance(branches, list) or not branches:
            ctx.errors.append('%s: "%s" must be a non-empty array, got %s'
                              % (path, keyword, _show(branches)))
            continue
        results = [_subcheck(value, branch, path, ctx) for branch in branches]
        matched = [r for r in results if not r.errors and not r.unchecked]
        undecided = [r for r in results if not r.errors and r.unchecked]
        if keyword == "anyOf":
            if matched:
                continue
            if undecided:
                ctx.unchecked.append(
                    (path, '"anyOf" could not be decided: %s'
                           % "; ".join(reason for r in undecided
                                       for _, reason in r.unchecked)))
                continue
            ctx.errors.append('%s: %s matches none of the %d "anyOf" branches'
                              % (path, _show(value), len(branches)))
        else:
            if undecided:
                ctx.unchecked.append(
                    (path, '"oneOf" could not be decided: %s'
                           % "; ".join(reason for r in undecided
                                       for _, reason in r.unchecked)))
                continue
            if len(matched) != 1:
                ctx.errors.append(
                    '%s: %s matches %d of the %d "oneOf" branches, expected exactly 1'
                    % (path, _show(value), len(matched), len(branches)))

    if "not" in schema:
        sub = _subcheck(value, schema["not"], path, ctx)
        if sub.unchecked:
            ctx.unchecked.append(
                (path, '"not" could not be decided: %s'
                       % "; ".join(reason for _, reason in sub.unchecked)))
        elif not sub.errors:
            ctx.errors.append('%s: %s must not match the "not" schema'
                              % (path, _show(value)))


def _validate_object(value, schema, path, ctx):
    required = schema.get("required", [])
    if isinstance(required, list):
        for key in required:
            if key not in value:
                ctx.errors.append("%s: missing required key %r" % (path, key))
    elif "required" in schema:
        # Not a list: iterating it would silently check its characters.
        _malformed(ctx, path, "required", "must be an array, got %s" % _show(required))

    # `.get(key, default)`, never `.get(key) or default`: the `or` form swallows
    # a falsy-but-malformed value ([], null, 0, "", false) before the isinstance
    # check can report it, so {"properties": []} came back clean while
    # {"properties": ["a"]} was caught. That is a guard that silently no-ops --
    # the exact shape of bug this module was widened to remove.
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        _malformed(ctx, path, "properties", "must be an object, got %s" % _show(properties))
        properties = {}
    pattern_properties = schema.get("patternProperties", {})
    compiled = []
    if isinstance(pattern_properties, dict):
        for raw, subschema in pattern_properties.items():
            regex = _compiled(ctx, path, "patternProperties", raw)
            if regex is not None:
                compiled.append((regex, raw, subschema))
    else:
        _malformed(ctx, path, "patternProperties",
                   "must be an object, got %s" % _show(pattern_properties))

    for key, subschema in properties.items():
        if key in value:
            _validate(value[key], subschema, "%s.%s" % (path, key), ctx)

    for key, item in value.items():
        matched_pattern = False
        for regex, raw, subschema in compiled:
            if regex.search(key):
                matched_pattern = True
                _validate(item, subschema, "%s.%s" % (path, key), ctx)
        if key in properties or matched_pattern:
            continue
        if "additionalProperties" not in schema:
            continue
        extra = schema["additionalProperties"]
        if extra is False:
            ctx.errors.append('%s: %r is not an allowed property '
                              '("additionalProperties" is false)' % (path, key))
        elif extra is True:
            continue
        elif isinstance(extra, dict):
            _validate(item, extra, "%s.%s" % (path, key), ctx)
        else:
            _malformed(ctx, path, "additionalProperties",
                       "must be a schema or a boolean, got %s" % _show(extra))
            break

    if "propertyNames" in schema:
        names_schema = schema["propertyNames"]
        if isinstance(names_schema, (dict, bool)):
            for key in value:
                _validate(key, names_schema, "%s.%s (property name)" % (path, key), ctx)
        else:
            # Same guard as `properties`/`patternProperties` above: `.get(kw) is
            # not None` cannot tell "absent" from "explicitly null", so
            # {"propertyNames": null} used to check nothing and say nothing.
            _malformed(ctx, path, "propertyNames",
                       "must be a schema, got %s" % _show(names_schema))

    minimum = _count_bound(ctx, path, schema, "minProperties")
    if minimum is not None and len(value) < minimum:
        ctx.errors.append('%s: has %d properties, "minProperties" is %d'
                          % (path, len(value), minimum))
    maximum = _count_bound(ctx, path, schema, "maxProperties")
    if maximum is not None and len(value) > maximum:
        ctx.errors.append('%s: has %d properties, "maxProperties" is %d'
                          % (path, len(value), maximum))


def _validate_array(value, schema, path, ctx):
    # Check array cardinality before descending into item schemas or comparing
    # every pair for ``uniqueItems``. A model can ignore a bounded response
    # schema, and continuing after a known bound violation turns one rejected
    # response into unbounded recursive work or O(n^2) equality checks.
    minimum = _count_bound(ctx, path, schema, "minItems")
    if minimum is not None and len(value) < minimum:
        ctx.errors.append('%s: has %d items, "minItems" is %d' % (path, len(value), minimum))
    maximum = _count_bound(ctx, path, schema, "maxItems")
    if maximum is not None and len(value) > maximum:
        ctx.errors.append('%s: has %d items, "maxItems" is %d'
                          % (path, len(value), maximum))
        return

    unique = schema.get("uniqueItems", _MISSING)
    if unique is not _MISSING and not isinstance(unique, bool):
        _malformed(ctx, path, "uniqueItems", "must be a boolean, got %s" % _show(unique))
    elif unique:
        host_cap = ctx.max_unique_items
        if host_cap is not None and len(value) > host_cap:
            ctx.errors.append(
                '%s: has %d items, host uniqueItems validation cap is %d'
                % (path, len(value), host_cap)
            )
            return

    if "items" in schema:
        items_schema = schema["items"]
        if isinstance(items_schema, list):
            # Draft-07 tuple form (one schema per position). Legal, and something a
            # decoder will honour -- this module just cannot apply it, so it is a
            # gap rather than a reason to reject the data.
            _uncheckable(ctx, path, "items",
                         "is a per-position tuple, which this verifier does not apply")
        elif isinstance(items_schema, (dict, bool)):
            for i, item in enumerate(value):
                _validate(item, items_schema, "%s[%d]" % (path, i), ctx)
        else:
            # Same guard as `properties`/`patternProperties`: `.get(kw) is not
            # None` cannot tell "absent" from "explicitly null", so
            # {"items": null} used to check nothing and say nothing.
            _malformed(ctx, path, "items",
                       "must be a schema or an array of schemas, got %s" % _show(items_schema))

    if unique is True:
        for i, item in enumerate(value):
            for j in range(i + 1, len(value)):
                if _json_equal(item, value[j]):
                    ctx.errors.append('%s: items %d and %d are equal, '
                                      '"uniqueItems" is true' % (path, i, j))
                    return


def _validate_string(value, schema, path, ctx):
    minimum = _count_bound(ctx, path, schema, "minLength")
    if minimum is not None and len(value) < minimum:
        ctx.errors.append('%s: is %d characters, "minLength" is %d'
                          % (path, len(value), minimum))
    maximum = _count_bound(ctx, path, schema, "maxLength")
    if maximum is not None and len(value) > maximum:
        ctx.errors.append('%s: is %d characters, "maxLength" is %d'
                          % (path, len(value), maximum))
    if "pattern" in schema:
        raw = schema["pattern"]
        regex = _compiled(ctx, path, "pattern", raw)
        if regex is not None and regex.search(value) is None:
            ctx.errors.append('%s: %s does not match "pattern" %s'
                              % (path, _show(value), _show(raw)))


def _validate_number(value, schema, path, ctx):
    def _bound(keyword):
        limit = schema.get(keyword, _MISSING)
        if limit is _MISSING:
            return None
        if isinstance(limit, bool) or not isinstance(limit, (int, float)):
            _malformed(ctx, path, keyword, "must be a number, got %s" % _show(limit))
            return None
        return limit

    limit = _bound("minimum")
    if limit is not None and value < limit:
        ctx.errors.append('%s: %s is below "minimum" %s' % (path, _show(value), limit))
    limit = _bound("maximum")
    if limit is not None and value > limit:
        ctx.errors.append('%s: %s is above "maximum" %s' % (path, _show(value), limit))
    limit = _bound("exclusiveMinimum")
    if limit is not None and value <= limit:
        ctx.errors.append('%s: %s is not above "exclusiveMinimum" %s'
                          % (path, _show(value), limit))
    limit = _bound("exclusiveMaximum")
    if limit is not None and value >= limit:
        ctx.errors.append('%s: %s is not below "exclusiveMaximum" %s'
                          % (path, _show(value), limit))
    limit = _bound("multipleOf")
    if limit is not None:
        if limit <= 0:
            _malformed(ctx, path, "multipleOf", "must be positive, got %s" % _show(limit))
        else:
            quotient = value / limit
            if abs(quotient - round(quotient)) > 1e-9:
                ctx.errors.append('%s: %s is not a multiple of %s'
                                  % (path, _show(value), limit))


def _validate(value, schema, path, ctx):
    """Depth-bounded entry to `_validate_node`. Everything recurses through
    here, so the bound cannot be bypassed by a new call site."""
    if ctx.depth >= MAX_DEPTH:
        ctx.unchecked.append(
            (path, "nesting is more than %d levels deep here, which is deeper "
                   "than this verifier walks, so nothing below was checked"
                   % MAX_DEPTH))
        return
    ctx.depth += 1
    try:
        _validate_node(value, schema, path, ctx)
    finally:
        ctx.depth -= 1


def _validate_node(value, schema, path, ctx):
    """Check value against schema, filling ctx's two channels."""
    # A boolean schema is legal JSON Schema: true accepts everything, false
    # rejects everything.
    if schema is True:
        return
    if schema is False:
        ctx.errors.append("%s: this schema rejects every value" % path)
        return
    if not isinstance(schema, dict):
        # Both channels: it is a broken schema (an error) *and* the reason
        # nothing under it was examined (a gap). A caller told only "this is
        # invalid" would still not know the subtree went unlooked-at.
        ctx.errors.append("%s: schema node must be an object, got %r" % (path, schema))
        ctx.unchecked.append(
            (path, "schema node is not an object, so nothing here was checked"))
        return

    if "$ref" in schema:
        _validate_ref(value, schema["$ref"], path, ctx)

    # Coverage, first half: anything this module does not implement. Computed as
    # the complement of CHECKED_KEYWORDS, so a keyword nobody anticipated is
    # reported rather than assumed harmless. Recorded BEFORE the type check
    # returns, because "at least this was wrong" is not the same claim as "this
    # is all that was wrong" -- a rejected value must still disclose what else
    # at that node went unexamined.
    unenforced = sorted(set(schema) - CHECKED_KEYWORDS - ANNOTATION_KEYWORDS)
    if unenforced:
        ctx.unchecked.append((path, "%s not checked" % ", ".join(unenforced)))

    if "type" in schema:
        # `.get("type") is not None` cannot tell "absent" from "explicitly
        # null", so {"type": null} used to check nothing and say nothing; the
        # membership test below routes it into the "unknown schema type" path,
        # which already fills both channels.
        declared = schema["type"]
        names = declared if isinstance(declared, list) else [declared]
        unknown = [n for n in names
                   if not isinstance(n, str) or n not in _TYPE_CHECKS]
        if unknown or not names:
            ctx.errors.append("%s: unknown schema type %r" % (path, declared))
            ctx.unchecked.append(
                (path, "type %r is not one this verifier understands, so nothing "
                       "at this node was checked" % (declared,)))
            return
        if not any(_TYPE_CHECKS[n](value) for n in names):
            ctx.errors.append("%s: expected type %s, got %s"
                              % (path, " or ".join(names), type(value).__name__))
            # Nested checks below would be meaningless on a type mismatch, and
            # the mismatch is already a hard failure -- no silence to misread.
            return

    _validate_combinators(value, schema, path, ctx)

    if "enum" in schema:
        options = schema["enum"]
        if not isinstance(options, list):
            ctx.errors.append('%s: "enum" must be an array, got %s' % (path, _show(options)))
        elif not any(_json_equal(value, option) for option in options):
            ctx.errors.append('%s: %s is not one of the %d values allowed by "enum"'
                              % (path, _show(value), len(options)))
    if "const" in schema and not _json_equal(value, schema["const"]):
        ctx.errors.append('%s: %s is not the "const" value %s'
                          % (path, _show(value), _show(schema["const"])))

    # Coverage, second half: keywords that are implemented but could not apply,
    # because the value is of another JSON type. This is the case that made a
    # missing "type" dangerous -- {"required": [...], "properties": {...}} over
    # a non-object used to check nothing and say nothing.
    applicable = (
        (_OBJECT_KEYWORDS, isinstance(value, dict), _validate_object),
        (_ARRAY_KEYWORDS, isinstance(value, list), _validate_array),
        (_STRING_KEYWORDS, isinstance(value, str), _validate_string),
        (_NUMBER_KEYWORDS,
         isinstance(value, (int, float)) and not isinstance(value, bool),
         _validate_number),
    )
    inapplicable = []
    for keywords, applies, check_fn in applicable:
        present = [k for k in keywords if k in schema]
        if not present:
            continue
        if applies:
            check_fn(value, schema, path, ctx)
        else:
            inapplicable.extend(present)
    if inapplicable:
        ctx.unchecked.append(
            (path, "%s did not apply and so checked nothing: the value is %s"
                   % (", ".join(sorted(inapplicable)), _kind(value))))


def check(data, schema, *, max_unique_items=None):
    """Validate an already-parsed JSON value, returning both channels:
    CheckResult(errors, unchecked). `errors` are violations; `unchecked` is
    [(path, reason), ...] naming everything this module could not evaluate.
    Pure function, no I/O."""
    if (max_unique_items is not None and
            (isinstance(max_unique_items, bool)
             or not isinstance(max_unique_items, int)
             or max_unique_items < 0)):
        raise ValueError("max_unique_items must be a non-negative integer or None")
    ctx = _Ctx(schema, max_unique_items=max_unique_items)
    try:
        _validate(data, schema, "$", ctx)
    except RecursionError:
        # `MAX_DEPTH` bounds the schema descent, but a pathological *value*
        # (a deeply nested `enum` member reached by `_json_equal`) could still
        # exhaust the stack. Whatever was found so far stands; the rest becomes
        # a gap, so the promise that this module never raises is structural
        # rather than a claim about every input.
        ctx.unchecked.append(
            ("$", "this data or schema nests too deeply for this verifier to "
                  "walk, so it was not fully checked"))
    return CheckResult(ctx.errors, ctx.unchecked)


def validate(data, schema):
    """Validate an already-parsed JSON value against schema. Returns a list of
    error strings (empty list == valid). Pure function, no I/O -- usable on
    its own outside the verifier seam, e.g. to check config dicts in-process.

    Violations ONLY. An empty list means "nothing broke a keyword I enforce",
    which is not the same as "this document is valid" -- call `coverage_gaps()`
    (or `check()`) to see what went unchecked, or you are back to reading
    silence as a pass."""
    return check(data, schema).errors


def coverage_gaps(data, schema):
    """Everything about `schema` that `validate(data, schema)` did not actually
    enforce, as [(path, reason), ...]. Empty means the whole traversal was
    checked.

    Walks the data as well as the schema, because the traversal is
    data-dependent: `properties[key]` is only entered when the key is present
    and `items` only applies to elements that exist, so a report derived from
    the schema alone would claim coverage of branches nothing ever walked.

    Total by construction -- an unexpected error is itself reported as total
    non-coverage, since leaving the caller with no coverage information is the
    failure mode this exists to prevent."""
    try:
        return check(data, schema).unchecked
    except Exception as exc:            # pragma: no cover - defensive
        return [("$", "coverage could not be determined: %s" % exc)]


def _format_gaps(unchecked, limit=8):
    shown = ["%s (%s)" % (path, reason) for path, reason in unchecked[:limit]]
    remaining = len(unchecked) - len(shown)
    if remaining > 0:
        shown.append("and %d more" % remaining)
    return "; ".join(shown)


def json_schema_verify(artifact, spec=None):
    """Verifier-registry entrypoint: fn(artifact: str, spec: dict) -> Verdict.
    spec={"schema": {...}}. `artifact` is the raw JSON text to validate.
    Never raises -- invalid JSON or a missing schema is a failed Verdict, not
    VerifierUnavailable (nothing external could be "unavailable" here). Deep
    nesting is part of that promise: the descent is bounded by `MAX_DEPTH` and
    `check` absorbs a RecursionError, so a document `json.loads` can parse can
    never come back out of here as an exception.

    Fails closed on ignorance: a Verdict has no third state, and passed=True is
    a claim that the data was checked, so a schema carrying anything this module
    cannot evaluate returns passed=False saying which part -- distinguishable
    from a violation by the reason text, and never conflated with it."""
    spec = spec or {}
    schema = spec.get("schema")
    if schema is None:
        return Verdict(False, "no schema provided", "spec['schema'] was missing or None")

    try:
        data = json.loads(artifact)
    except json.JSONDecodeError as e:
        return Verdict(False, "invalid json: %s" % e, str(e))

    errors, unchecked = check(data, schema)
    if errors:
        reason = errors[0] if len(errors) == 1 else "%d schema violations" % len(errors)
        detail = "\n".join(errors)
        if unchecked:
            # "at least this was wrong" is not "this is all that was wrong".
            detail += "\nunverified: %s" % _format_gaps(unchecked)
        return Verdict(False, reason, detail)
    if unchecked:
        summary = _format_gaps(unchecked)
        reason = ("cannot verify: %s" % summary if len(unchecked) == 1
                  else "cannot verify: %d parts of this schema are unchecked" % len(unchecked))
        return Verdict(False, reason,
                       "no violation found, but this verifier does not check:\n%s" % summary)
    return Verdict(True, "valid", "matches schema")
