"""grounded_extraction -- structured extraction that has to cite its source.

The local 7B is measured at roughly 53% caller-judged good, and the failure is
not evenly spread: handed every fact it needs, it transforms them well; asked to
*supply* a fact it was not given, it invents one and presents it as fact. A
Win32 ROP2 table came back with 10 of 16 rows wrong. Nothing about the output
looked wrong.

Task 1 made offload's *shape* checkable by constraining it with a JSON Schema.
A shape check cannot see this failure at all: ``{"birth_year": 1852}`` is
perfectly schema-valid and perfectly false. This module adds the missing half
for the extraction case, where the answer is supposed to come out of a document
the caller already has:

* :func:`grounded_schema` rewrites the caller's field schema so the model must
  return, for every field, both a ``value`` and the ``quote`` -- the span of
  source text -- that states it;
* :func:`verify_grounding` then checks each quote against the source by literal
  substring. A field whose quote is not in the source is rejected, by name.

The check is mechanical on purpose. Asking a model to produce its own evidence
invites it to invent the evidence too, so nothing here weighs whether a citation
is *plausible* -- it only asks whether that exact text is in the document the
caller supplied. String comparison cannot be talked round.

WHAT THIS DOES NOT CATCH -- read this before trusting a result
--------------------------------------------------------------
**A quote being present does not mean it supports the value attached to it.**
A model can copy a real sentence out of the source and hang a fabricated value
off it, and every check here passes. Given "Ada Lovelace was born in London on
10 December 1815", the field ``birth_year: 1852`` quoted against
``"born in London on 10 December 1815"`` is *accepted*: the span is real. The
guarantee is "this field points at text that genuinely exists in your source",
not "your source says this". It narrows invention from anywhere to somewhere in
the document, and it makes the citation cheap for a human or a later checker to
land on -- it does not eliminate invention. Deciding whether a span *entails* a
value is a judgement, and a judgement is exactly what this module refuses to
make, because a judgement made by the same class of model is the thing that was
unreliable in the first place.

Two narrower limits, stated rather than glossed:

* **Spans are text, not offsets.** The model returns the quoted text and the
  offset is computed here. A model asked to count characters gets it wrong, and
  a claimed offset would have to be re-derived from the text anyway. The cost is
  that a phrase occurring more than once is ambiguous: ``quote_occurrences``
  reports how many times it occurs and ``quote_offset`` is the first, so a
  caller can see when the span does not identify a unique place.
* **Comparison is exact -- there is no normalisation whatsoever.** Not
  whitespace, not case, not unicode. Every normalisation is a widening of the
  set of strings an invented span is allowed to match, and this check is worth
  having only in proportion to how narrow it is. The price is real: a model that
  re-wraps a line or straightens a quotation mark while copying is rejected even
  though it was being honest. That error runs in the safe direction -- a false
  rejection costs a retry, a false acceptance is the failure this exists to
  stop.
"""

VALUE_KEY = "value"
QUOTE_KEY = "quote"

# How much of an offending span a rejection message repeats back.
_PREVIEW_CHARS = 80

EXTRACTION_SYSTEM = (
    "You extract facts that are stated in the SOURCE text, and nothing else. "
    "Every field is an object with two keys: \"value\", the extracted value, and "
    "\"quote\", the span of the SOURCE that states it. Copy the quote CHARACTER "
    "FOR CHARACTER out of the SOURCE: do not correct spelling, punctuation or "
    "capitalisation, do not change spacing or line breaks, do not paraphrase, and "
    "do not join text from two different places into one quote. Use nothing you "
    "know from outside the SOURCE. If the SOURCE does not state a field, you have "
    "no quote for it -- omit it if it is optional. A field whose quote is not "
    "found in the SOURCE is thrown away."
)


class GroundingError(ValueError):
    """An extraction that could not be tied back to the supplied source."""


def _preview(text):
    if len(text) <= _PREVIEW_CHARS:
        return repr(text)
    return repr(text[:_PREVIEW_CHARS] + "...")


def grounded_schema(schema):
    """Rewrite a flat field schema so every field must carry its evidence.

    ``{"properties": {"name": {"type": "string"}}}`` becomes a schema requiring
    ``{"name": {"value": <the caller's subschema>, "quote": {"type": "string"}}}``,
    so the decoder itself cannot produce a bare value with no citation.

    The caller's ``required`` list is honoured as written rather than promoted to
    "all fields". A field the source never mentions cannot be grounded, so
    demanding one would be demanding an invention; leaving it optional lets the
    model return nothing, which is the honest answer.

    Only the top level is grounded. A nested object or array is still validated
    against the caller's subschema, but it is cited as a whole rather than
    field-by-field -- deep shapes are also where a 3B/7B does worst.
    """
    if not isinstance(schema, dict):
        raise GroundingError(
            "extraction schema must be a JSON object, got %s" % type(schema).__name__
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise GroundingError(
            "extraction schema needs a non-empty \"properties\" object naming the "
            "fields to extract; there is nothing to ground without it"
        )
    required = schema.get("required")
    if not isinstance(required, list):
        required = list(properties)
    unknown = [name for name in required if name not in properties]
    if unknown:
        # Filtering these out turned a caller's typo into a clean success: a
        # `required` naming only fields that do not exist produced a schema with
        # nothing in it, and the tool answered `{"fields": {}}` as though the
        # document had been read and none of the facts were in it. A caller
        # cannot tell that apart from an honest empty result, which makes it the
        # same shape as a guard that silently no-ops. `_parse_schema_arg` already
        # refuses a malformed schema outright rather than running the call
        # unconstrained while the caller believes it was constrained; this is
        # that rule applied one layer up.
        raise GroundingError(
            "extraction schema lists %s in \"required\" but does not define %s in "
            "\"properties\"; a field that is required and undefined can be "
            "neither extracted nor grounded"
            % (", ".join(repr(name) for name in unknown),
               "them" if len(unknown) > 1 else "it")
        )
    return {
        "type": "object",
        "required": [name for name in required if name in properties],
        "properties": {
            name: {
                "type": "object",
                "required": [VALUE_KEY, QUOTE_KEY],
                "properties": {
                    VALUE_KEY: subschema if isinstance(subschema, dict) else {},
                    QUOTE_KEY: {"type": "string"},
                },
            }
            for name, subschema in properties.items()
        },
    }


def extraction_prompt(source, task=""):
    """The user turn: the instruction, then the source, fenced and unmodified."""
    parts = []
    if task and task.strip():
        parts.append(task.strip())
    parts.append(
        "Extract the requested fields from the SOURCE below. Every quote must be "
        "copied out of it exactly."
    )
    parts.append("--- SOURCE ---\n%s\n--- END SOURCE ---" % source)
    return "\n\n".join(parts)


def verify_grounding(data, source):
    """Check every field's quote against `source`, or raise naming the failures.

    Returns ``{field: {"value", "quote", "quote_offset", "quote_occurrences"}}``.
    ``quote_offset`` and ``quote_occurrences`` are computed here from the source,
    never taken from the model.

    Every field is checked before anything is raised, so one bad citation does
    not hide the others. Nothing is repaired, dropped or re-asked: a response
    with an ungrounded field is rejected whole, because the alternative is
    handing back a partial answer that reads like a complete one.
    """
    if not isinstance(data, dict):
        raise GroundingError(
            "extraction did not come back as a JSON object (got %s), so no field "
            "could be grounded" % type(data).__name__
        )
    if not isinstance(source, str) or not source:
        raise GroundingError(
            "no source text to ground against; every span would fail by default"
        )

    grounded = {}
    problems = []
    for name, entry in data.items():
        if not isinstance(entry, dict) or VALUE_KEY not in entry or QUOTE_KEY not in entry:
            problems.append(
                "%r did not come back as a {\"%s\": ..., \"%s\": ...} pair"
                % (name, VALUE_KEY, QUOTE_KEY)
            )
            continue
        quote = entry[QUOTE_KEY]
        if not isinstance(quote, str):
            problems.append(
                "%r cited a %s where a quoted span was required"
                % (name, type(quote).__name__)
            )
            continue
        if not quote.strip():
            # Every source contains the empty string, so accepting this would
            # make the whole check vacuous for the price of one blank field.
            problems.append("%r cited a blank span, which any source trivially contains" % name)
            continue
        occurrences = source.count(quote)
        if not occurrences:
            problems.append(
                "%r cited a span that does not appear in the source: %s"
                % (name, _preview(quote))
            )
            continue
        grounded[name] = {
            VALUE_KEY: entry[VALUE_KEY],
            QUOTE_KEY: quote,
            "quote_offset": source.find(quote),
            "quote_occurrences": occurrences,
        }

    if problems:
        raise GroundingError("ungrounded extraction: %s" % "; ".join(problems))
    return grounded
