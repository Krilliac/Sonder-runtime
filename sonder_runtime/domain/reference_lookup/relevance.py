"""Is the best retrieved passage close enough to the question to be handed to the model?

The search is an OR over every word of the question, so it finds *something*
for almost any text: a long question about another subject shares enough
common words with the library to bring back its five nearest passages, which
then fill the prompt and are listed as Sources although they have nothing to
do with the answer.

The measure used here is coverage: the share of the query's search words
that one passage holds, each word weighted by how rare it is in the library
(BM25's inverse document frequency, so "server" counts for little and a word
the library never uses counts fully against the passage).  A passage about the
question's subject holds a good part of its distinctive words; a passage found
through common words does not.  The caller measures terms without a space
only (a word, or an identifier such as "t1003.001"): a
multi-word phrase a model wrote is counted through its words, because a phrase
the library never holds verbatim would otherwise weigh most and the result
would turn on how the model happened to phrase its terms (one covered question
fell to 0.19 that way under a reworded request).

``MIN_COVERAGE`` was set from the security reference library (8,908
passages) on 2026-11-13, over six sets of search terms: the question's own
words alone, and the terms each of two local models produced for three
wordings of the search-terms request.

* 44 questions the library covers, the two longest about 300 words: the best
  passage covered 0.24 to 1.00 in every run, so none lost its passages;
* two 420 to 450 word design questions about other subjects (the reported
  case): 0.15 in every run, so both lost them;
* twelve short questions about other subjects: 0.09 to 0.46; five of them
  stayed above the floor with the larger model's terms, ten with the smaller
  model's.

``UNRELATED_MIN_COVERAGE`` is for those short questions.  The search-terms
call is also asked whether the question is in the library's subjects, and a
verdict of UNRELATED raises the floor for that turn.  Measured the same day
by replaying both models' recorded replies through the lookup:

* larger model: no covered question judged UNRELATED; twelve of the fourteen
  unrelated questions lost their passages (five kept them under the plain
  floor, two with the verdict: one judged unrelated at 0.46, one judged
  related);
* smaller model: five of 44 covered questions wrongly judged UNRELATED, at
  0.49 to 1.00, so none lost its passages; nine of fourteen unrelated
  questions lost theirs (three were judged related, two reached 0.45).

The verdict is therefore never used alone, and the raised floor sits under
the lowest wrongly judged covered question with little room: eight covered
questions measured under 0.40 with the smaller model's terms, and one of them
judged UNRELATED would be answered without passages (the Sources block then
says none was close enough).  A reply without a verdict leaves the floor
where it was.  The longer request can change the terms the model writes, and
through them the passages found; the six term sets above include it.

So the floors remove the clear misses and keep every measured hit.  They do
not catch a short unrelated question that the model judges related or whose
best passage reaches the raised floor; the Sources block says when an answer
cites none of its passages.  Pure functions; nothing here reads the index.
"""
from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping

MIN_COVERAGE = 0.20
# The floor for a turn whose search-terms reply judged the question outside the
# library's subjects (``terms.split_scope``).  The verdict alone is not trusted:
# the smaller of the two measured models gave it to five of 44 covered questions.
UNRELATED_MIN_COVERAGE = 0.40


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
