"""Campaign corpus containing two verbatim datasets from ``server.py``.

``CAMPAIGN_TASKS`` contains 12 two-string tuples; ``REPO_REPAIR_TASKS``
contains 10 three-string tuples. These mutable lists are shared with existing
server aliases for compatibility. Provider selection, execution, and security
gates remain in server orchestration.
"""
from __future__ import annotations


CAMPAIGN_TASKS: list[tuple[str, str]] = [
    ("hello", "print exactly: sonder-ok"),
    ("sum", "compute 12 + 30 and print exactly: 42"),
    ("loop", "print the numbers 1, 2, and 3 each on its own line"),
    ("string", "reverse the string 'sonder' and print exactly: rednos"),
    ("branch", "if 17 is prime print exactly: prime"),
    ("list", "compute the sum of [2, 4, 6, 8] and print exactly: 20"),
    # Harder algorithmic tier: these force the failure classes small local
    # models actually exhibit (incomplete map initialization, eviction-order
    # bookkeeping, interval bookkeeping, stack discipline) so campaign
    # records and distilled lessons carry real signal, not just boilerplate.
    ("toposort",
     "topologically sort the directed edges d->a, a->b, b->c, a->c (an edge "
     "u->v means u comes before v; include every node) and print the only "
     "valid order as space-separated letters on one line, like: w x y z"),
    ("lru",
     "simulate an LRU cache with capacity 2 and operations put(1,10), "
     "put(2,20), get(1), put(3,30) (this evicts the least recently used "
     "key), get(2), get(3); print the three get results on one line "
     "separated by single spaces, using -1 for a miss"),
    ("intervals",
     "merge the overlapping intervals [1,3] [2,6] [8,10] [9,12] and print "
     "the merged intervals on one line as start-end pairs separated by a "
     "single space, like 5-7 9-11"),
    ("balanced",
     # Two harness lessons are baked into this wording. (1) The pass check is
     # a containment check, so wrong verdicts must not contain the expected
     # text as a substring: 'ok'/'bad' cannot embed each other the way
     # 'valid'/'invalid' can. (2) Bracket literals must be quoted and
     # numbered, never wrapped in bare parentheses — models read those outer
     # parens as punctuation and silently drop them, then get marked wrong
     # for answering a different question than the one intended.
     "decide whether the brackets are balanced in each of these three "
     "strings, given exactly between the quotes: "
     "1) \"([]{})\"  2) \"([)]\"  3) \"(((\" . "
     "Print ok if that string's brackets are balanced or bad if they are "
     "not, one verdict per line, in that order"),
    ("wordfreq",
     "count word frequencies in the sentence 'the quick the lazy the end' "
     "and print the most frequent word and its count as word:count"),
    ("fib",
     "print the 20th Fibonacci number where fib(1)=1 and fib(2)=1"),
]


REPO_REPAIR_TASKS: list[tuple[str, str, str]] = [
    ("offbyone",
     "def total(values):\n"
     "    result = 0\n"
     "    for index in range(len(values) - 1):\n"
     "        result += values[index]\n"
     "    return result\n",
     "from module import total\n\n\n"
     "def test_total_includes_every_value():\n"
     "    assert total([1, 2, 3, 4]) == 10\n\n\n"
     "def test_total_single_value():\n"
     "    assert total([7]) == 7\n\n\n"
     "def test_total_empty():\n"
     "    assert total([]) == 0\n"),
    ("boundary",
     "def bulk_discount(quantity):\n"
     "    # spec: orders of 10 or more get the discount\n"
     "    return 0.1 if quantity > 10 else 0.0\n",
     "from module import bulk_discount\n\n\n"
     "def test_discount_at_threshold():\n"
     "    assert bulk_discount(10) == 0.1\n\n\n"
     "def test_discount_above_threshold():\n"
     "    assert bulk_discount(11) == 0.1\n\n\n"
     "def test_no_discount_below():\n"
     "    assert bulk_discount(9) == 0.0\n"),
    ("mutabledefault",
     "def add_tag(tag, tags=[]):\n"
     "    tags.append(tag)\n"
     "    return tags\n",
     "from module import add_tag\n\n\n"
     "def test_fresh_list_per_call():\n"
     "    assert add_tag('a') == ['a']\n"
     "    assert add_tag('b') == ['b']\n\n\n"
     "def test_explicit_list_still_appends():\n"
     "    existing = ['x']\n"
     "    assert add_tag('y', existing) == ['x', 'y']\n"),
    ("missingkey",
     "def word_counts(words):\n"
     "    counts = {}\n"
     "    for word in words:\n"
     "        counts[word] += 1\n"
     "    return counts\n",
     "from module import word_counts\n\n\n"
     "def test_counts_new_words():\n"
     "    assert word_counts(['a', 'b', 'a']) == {'a': 2, 'b': 1}\n\n\n"
     "def test_counts_empty():\n"
     "    assert word_counts([]) == {}\n"),
    ("numericsort",
     "def sort_ids(ids):\n"
     "    # ids arrive as decimal strings; callers need numeric order\n"
     "    return sorted(ids)\n",
     "from module import sort_ids\n\n\n"
     "def test_numeric_not_lexicographic():\n"
     "    assert sort_ids(['10', '2', '1']) == ['1', '2', '10']\n\n\n"
     "def test_already_sorted():\n"
     "    assert sort_ids(['1', '2']) == ['1', '2']\n"),
    # A second tier of bug classes. The first five became memorised - 10/10 on
    # every nightly run, which measures recall rather than repair - so these
    # cover failure modes the originals miss: aliasing a caller's object,
    # integer division, short-circuit ordering against None, accumulating in
    # the wrong scope, and an exclusive range boundary.
    ("aliasing",
     "def with_defaults(config):\n"
     "    # must not disturb the caller's dict\n"
     "    merged = config\n"
     "    merged.setdefault('retries', 3)\n"
     "    return merged\n",
     "from module import with_defaults\n\n\n"
     "def test_caller_dict_is_untouched():\n"
     "    original = {'host': 'localhost'}\n"
     "    result = with_defaults(original)\n"
     "    assert result == {'host': 'localhost', 'retries': 3}\n"
     "    assert original == {'host': 'localhost'}\n\n\n"
     "def test_explicit_value_wins():\n"
     "    assert with_defaults({'retries': 9})['retries'] == 9\n"),
    ("intdivision",
     "def average(values):\n"
     "    return sum(values) // len(values)\n",
     "from module import average\n\n\n"
     "def test_average_is_not_truncated():\n"
     "    assert average([1, 2]) == 1.5\n\n\n"
     "def test_exact_average_stays_exact():\n"
     "    assert average([2, 4]) == 3\n"),
    ("noneguard",
     "def name_length(user):\n"
     "    # user may be None, or may have no name set\n"
     "    return len(user['name']) if user['name'] else 0\n",
     "from module import name_length\n\n\n"
     "def test_missing_user_is_zero():\n"
     "    assert name_length(None) == 0\n\n\n"
     "def test_empty_name_is_zero():\n"
     "    assert name_length({'name': ''}) == 0\n\n\n"
     "def test_real_name_is_counted():\n"
     "    assert name_length({'name': 'ada'}) == 3\n"),
    ("wrongscope",
     "def group_lengths(words):\n"
     "    # one bucket per length\n"
     "    buckets = {}\n"
     "    found = []\n"
     "    for word in words:\n"
     "        found.append(word)\n"
     "        buckets[len(word)] = found\n"
     "    return buckets\n",
     "from module import group_lengths\n\n\n"
     "def test_each_bucket_holds_only_its_own_words():\n"
     "    assert group_lengths(['a', 'bb', 'cc']) == {1: ['a'], 2: ['bb', 'cc']}\n\n\n"
     "def test_empty_input():\n"
     "    assert group_lengths([]) == {}\n"),
    ("rangeend",
     "def inclusive_range(start, end):\n"
     "    # callers expect both endpoints included\n"
     "    return list(range(start, end))\n",
     "from module import inclusive_range\n\n\n"
     "def test_end_is_included():\n"
     "    assert inclusive_range(1, 4) == [1, 2, 3, 4]\n\n\n"
     "def test_single_value_range():\n"
     "    assert inclusive_range(2, 2) == [2]\n"),
]
