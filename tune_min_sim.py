"""tune_min_sim — recalibrate retriever.DEFAULT_MIN_SIM against the live corpus.

The gate in retriever.retrieve() keeps a candidate lesson only if its cosine to
the query clears min_sim. Calibrated once against the tiny game-ladder corpus,
0.65 now over-cuts real hits (e.g. the sql-injection lesson scores ~0.650).

This sweeps thresholds against two probe sets and reports, per threshold:
  * recall     = fraction of POSITIVE coding intents that still return >=1 lesson
  * noise-rate = fraction of OFF-DOMAIN probes that would wrongly return a lesson
Pick the highest recall while keeping noise-rate ~0. Embeddings only -> no GPU
generation, fast.

Run: ./venv/Scripts/python.exe tune_min_sim.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import embeddings  # noqa: E402
import memory_store  # noqa: E402

# Natural-language coding intents that SHOULD match a seeded lesson.
POSITIVES = [
    "prevent sql injection in a query",
    "design a scalable cache with invalidation",
    "avoid a mutable default argument bug",
    "get the kth largest element with a heap",
    "merge overlapping intervals",
    "retry an http request with exponential backoff",
    "debug a flaky test that only fails sometimes",
    "recover a lost commit in git",
    "avoid deadlock when acquiring multiple locks",
    "reverse a linked list or string in place",
    "topological sort of a dependency graph",
    "memoize an expensive recursive function",
    "parse an arithmetic expression without eval",
    "handle timezone-aware datetimes correctly",
    "group anagrams together",
    "detect a cycle in a graph",
    "safely acquire and release a lock with a context manager",
    "dynamic programming for edit distance",
    "hash a password for storage",
    "profile a slow function before optimizing",
    "sliding window maximum in linear time",
    "cooperative super() in a diamond inheritance",
]

# Clearly off-domain text that should return NOTHING from a coding-lesson store.
NEGATIVES = [
    "what is the weather forecast for Paris tomorrow",
    "a recipe for banana bread with walnuts",
    "the history of the Roman empire",
    "my favorite color is blue and I like cats",
    "book a table for two at an italian restaurant",
    "lyrics to a pop song about summer",
    "how tall is Mount Everest",
    "the best hiking trails in Colorado",
    "who won the world cup in 2018",
    "symptoms of the common cold",
    "plan a birthday party for a ten year old",
    "the plot of a romance novel",
    "how to train a golden retriever puppy",
    "current stock price of a coffee company",
    "directions to the nearest gas station",
]


def top1_scores(conn, queries):
    lessons = [(l["text"], embeddings.from_blob(l["embedding"]))
               for l in memory_store.all_lessons(conn) if l["embedding"]]
    out = []
    for q in queries:
        qv = embeddings.embed(q)
        best = max((embeddings.cosine(qv, v) for _, v in lessons), default=0.0)
        out.append(best)
    return out


def main():
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.db")
    conn = memory_store.connect(db)
    try:
        pos = top1_scores(conn, POSITIVES)
        neg = top1_scores(conn, NEGATIVES)
    finally:
        conn.close()

    print("positives top-1 cosine: min=%.3f  median=%.3f  max=%.3f"
          % (min(pos), sorted(pos)[len(pos) // 2], max(pos)))
    print("negatives top-1 cosine: min=%.3f  median=%.3f  max=%.3f"
          % (min(neg), sorted(neg)[len(neg) // 2], max(neg)))
    print("\n thr   recall(pos)   noise(neg)   J=recall-noise")
    best = None
    for i in range(50, 71):
        t = i / 100.0
        recall = sum(1 for s in pos if s >= t) / len(pos)
        noise = sum(1 for s in neg if s >= t) / len(neg)
        j = recall - noise
        mark = ""
        if best is None or j > best[1] + 1e-9:
            best = (t, j, recall, noise)
        if best and abs(t - best[0]) < 1e-9:
            mark = "  <- best J"
        print(" %.2f      %.2f          %.2f         %+.2f%s" % (t, recall, noise, j, mark))
    print("\nbest J at thr=%.2f (recall=%.2f, noise=%.2f)" % (best[0], best[2], best[3]))
    # Also report the lowest threshold that still rejects ALL negatives.
    clean = [i / 100.0 for i in range(50, 71)
             if sum(1 for s in neg if s >= i / 100.0) == 0]
    if clean:
        t = min(clean)
        r = sum(1 for s in pos if s >= t) / len(pos)
        print("lowest zero-noise thr=%.2f (recall=%.2f)" % (t, r))


if __name__ == "__main__":
    main()
