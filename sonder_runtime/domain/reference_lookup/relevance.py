"""Is the best retrieved passage close enough to the question to be handed to the model?

The search is an OR over every word of the question, so it finds *something*
for almost any text: a long question about another subject shares enough
common words with the library to bring back its five nearest passages, which
then fill the prompt and are listed as Sources although they have nothing to
do with the answer.

The measure used here is coverage: the share of the query's search vocabulary
that one passage holds, each term weighted by how rare it is in the library
(BM25's inverse document frequency, so "server" counts for little and a term
the library never uses counts fully against the passage).  A passage about the
question's subject holds a good part of its distinctive words; a passage found
through common words does not.

``MIN_COVERAGE`` was set from the security reference library (8,908
passages) on 2026-11-13, three times over: with the search terms each of two
local models actually produced, and with the question's own words alone.

* 44 questions the library covers, the two longest about 300 words: the best
  passage covered 0.26 to 1.00 in every run, so none lost its passages, and
  the passages kept were the ones retrieved without a floor;
* two 420 to 450 word design questions about other subjects (the reported
  case): 0.15 in every run, so both lost them;
* twelve short questions about other subjects: 0.08 to 0.51; three, five or
  eight of them fell under the floor, depending on the terms.

So the floor removes the clear misses and keeps every measured hit.  It does
not catch a short unrelated question that happens to share a rare word with
the library; the Sources block says when an answer cites none of its passages.
Pure functions; nothing here reads the index.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping

MIN_COVERAGE = 0.20


def term_weight(passage_count: int, matching: int) -> float:
    """How much one term counts: BM25's inverse document frequency, always positive.

    ``matching`` is the number of passages holding the term; a term no passage
    holds gets the largest weight.
    """
    total = max(int(passage_count), 0)
    held = min(max(int(matching), 0), total)
    return math.log((total - held + 0.5) / (held + 0.5) + 1.0)


def coverage(weights: Mapping[str, float], held: Collection[str]) -> float:
    """The weighted share (0..1) of the terms in ``weights`` that are in ``held``."""
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    return sum(weight for term, weight in weights.items() if term in held) / total


def best_coverage(weights: Mapping[str, float], held_by_passage: Iterable[Collection[str]]) -> float:
    """The coverage of the passage that covers most; 0.0 without passages."""
    return max((coverage(weights, held) for held in held_by_passage), default=0.0)
