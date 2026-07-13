"""Near-duplicate lesson pruner. Stdlib only.

Clusters lessons in the memory_store by embedding cosine similarity and
reports (dry-run default) or deletes the redundant ones in each cluster,
keeping a single best representative.

Read-only against memory_store's schema -- the only mutation this module
performs is via memory_store.delete_lesson, and only when explicitly told
to apply a plan (dry_run=False / --apply). Building and reviewing a plan
never touches the database.
"""
import argparse

import embeddings
import memory_store

# Similarity floor for "near-duplicate". Deliberately conservative (well above
# retriever.DEFAULT_MIN_SIM=0.62, which is a *relevance* floor for retrieval,
# not a duplicate floor): two lessons about the same topic can legitimately
# share phrasing without being redundant. 0.93 targets true restatements.
DEFAULT_THRESHOLD = 0.93


def _load_lessons(conn):
    """All lessons with a stored embedding, decoded, oldest-first.

    Lessons with missing, malformed, non-finite, zero-norm, or dimension-
    inconsistent embeddings are skipped -- similarity cannot be judged safely
    for them. Provenance is loaded with each vector so clustering can keep
    incompatible embedding spaces isolated.
    """
    rows = conn.execute(
        "SELECT id, text, embedding, embedding_model, embedding_revision, "
        "embedding_dim, ts FROM lessons ORDER BY ts ASC, rowid ASC"
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if row["embedding"] is None:
            continue
        try:
            vector = embeddings.from_blob(row["embedding"])
        except (BufferError, OverflowError, TypeError, ValueError):
            continue
        if not embeddings.valid_vector(vector):
            continue
        stored_dim = row["embedding_dim"]
        if (
            isinstance(stored_dim, bool)
            or not isinstance(stored_dim, int)
            or stored_dim <= 0
            or stored_dim != len(vector)
        ):
            continue
        row["vector"] = vector
        out.append(row)
    return out


def _normalized_embedding_space(lesson):
    """Return the exact comparable embedding-space identity for a lesson."""
    return (
        embeddings.canonical_model_name(lesson.get("embedding_model")),
        str(lesson.get("embedding_revision") or "").strip(),
        len(lesson["vector"]),
    )


def _clusterable_lesson(lesson):
    """Whether a caller-supplied lesson is safe to pass to cosine math."""
    vector = lesson.get("vector")
    if not embeddings.valid_vector(vector):
        return False
    model, revision, _actual_dim = _normalized_embedding_space(lesson)
    if not model or not revision:
        return False
    if "embedding_dim" not in lesson:
        return True
    stored_dim = lesson["embedding_dim"]
    return (
        isinstance(stored_dim, int)
        and not isinstance(stored_dim, bool)
        and stored_dim > 0
        and stored_dim == len(vector)
    )


class _UnionFind:
    """Minimal disjoint-set for single-linkage clustering by id."""

    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_near_duplicates(lessons, threshold=DEFAULT_THRESHOLD, cosine_fn=embeddings.cosine):
    """Single-linkage clustering of lessons whose pairwise cosine >= threshold.

    O(n^2) comparisons -- fine for the hundreds-to-low-thousands of lessons
    this store holds; revisit (e.g. LSH/bucket by a cheap prefilter) if the
    corpus grows past ~10k. `lessons` is the shape _load_lessons returns
    (dicts with at least id/vector). Comparisons are restricted to the exact
    normalized (model, revision, actual dimension) embedding space. Returns
    only clusters with 2+ members (i.e. actual duplicate groups) -- singletons
    are dropped.
    """
    comparable = [lesson for lesson in lessons if _clusterable_lesson(lesson)]
    if not comparable:
        return []
    uf = _UnionFind([lesson["id"] for lesson in comparable])
    by_space = {}
    for lesson in comparable:
        by_space.setdefault(_normalized_embedding_space(lesson), []).append(lesson)
    for space_lessons in by_space.values():
        n = len(space_lessons)
        for i in range(n):
            for j in range(i + 1, n):
                if cosine_fn(
                    space_lessons[i]["vector"], space_lessons[j]["vector"]
                ) >= threshold:
                    uf.union(space_lessons[i]["id"], space_lessons[j]["id"])

    groups = {}
    for les in comparable:
        root = uf.find(les["id"])
        groups.setdefault(root, []).append(les)

    return [g for g in groups.values() if len(g) > 1]


def _max_pair_sim(cluster, cosine_fn=embeddings.cosine):
    best = 0.0
    for i in range(len(cluster)):
        for j in range(i + 1, len(cluster)):
            s = cosine_fn(cluster[i]["vector"], cluster[j]["vector"])
            if s > best:
                best = s
    return best


def choose_keeper(cluster):
    """Pick the survivor of a duplicate cluster.

    Longest text wins (assumed most detailed/specific restatement); ties
    break on earliest ts (prefer the original over a later paraphrase).
    """
    return sorted(
        cluster,
        key=lambda lesson: (-len(lesson["text"] or ""), lesson["ts"]),
    )[0]


def build_plan(conn, threshold=DEFAULT_THRESHOLD, cosine_fn=embeddings.cosine):
    """Dry-run prune plan: one entry per duplicate cluster, most-similar first.

    Each entry: {keeper_id, keeper_text, prune_ids, prune_texts, cluster_size,
    max_sim}. Nothing is deleted here -- see apply_plan / prune.
    """
    lessons = _load_lessons(conn)
    clusters = cluster_near_duplicates(lessons, threshold=threshold, cosine_fn=cosine_fn)
    plan = []
    for cluster in clusters:
        keeper = choose_keeper(cluster)
        losers = [lesson for lesson in cluster if lesson["id"] != keeper["id"]]
        plan.append({
            "keeper_id": keeper["id"],
            "keeper_text": keeper["text"],
            "prune_ids": [lesson["id"] for lesson in losers],
            "prune_texts": [lesson["text"] for lesson in losers],
            "cluster_size": len(cluster),
            "max_sim": round(_max_pair_sim(cluster, cosine_fn), 4),
        })
    plan.sort(key=lambda e: -e["max_sim"])
    return plan


def _truncate(text, n=70):
    text = text or ""
    return text if len(text) <= n else text[: n - 3] + "..."


def format_report(plan):
    """Human-readable dry-run summary, suitable for a CLI or a loop's log."""
    if not plan:
        return "No near-duplicate lessons found."
    total_prunable = sum(len(e["prune_ids"]) for e in plan)
    lines = ["%d duplicate cluster(s), %d lesson(s) prunable:" % (len(plan), total_prunable)]
    for e in plan:
        lines.append(
            "  keep %s (%r) -- prune %d dup(s) [max_sim=%.3f]"
            % (e["keeper_id"], _truncate(e["keeper_text"]), len(e["prune_ids"]), e["max_sim"])
        )
        for pid, ptext in zip(e["prune_ids"], e["prune_texts"]):
            lines.append("    - %s: %r" % (pid, _truncate(ptext)))
    return "\n".join(lines)


def apply_plan(conn, plan, delete_fn=memory_store.delete_lesson):
    """Delete every prune_id in plan via delete_fn. Returns count deleted."""
    deleted = 0
    for entry in plan:
        for lid in entry["prune_ids"]:
            if delete_fn(conn, lid):
                deleted += 1
    return deleted


def prune(conn, threshold=DEFAULT_THRESHOLD, dry_run=True, cosine_fn=embeddings.cosine,
          delete_fn=memory_store.delete_lesson):
    """End-to-end: build the plan, apply it unless dry_run. Returns (plan, deleted)."""
    plan = build_plan(conn, threshold=threshold, cosine_fn=cosine_fn)
    deleted = 0 if dry_run else apply_plan(conn, plan, delete_fn=delete_fn)
    return plan, deleted


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="memory.db", help="path to the sqlite memory store")
    ap.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help="cosine similarity floor for 'near-duplicate' (default %.2f)" % DEFAULT_THRESHOLD,
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="actually delete redundant lessons (default: dry-run report only)",
    )
    args = ap.parse_args()

    conn = memory_store.connect(args.db)
    plan, deleted = prune(conn, threshold=args.threshold, dry_run=not args.apply)
    print(format_report(plan))
    if args.apply:
        print("\nDeleted %d redundant lesson(s)." % deleted)
    else:
        total_prunable = sum(len(e["prune_ids"]) for e in plan)
        print("\n(dry-run: pass --apply to delete the %d prunable lesson(s) above)" % total_prunable)


if __name__ == "__main__":
    main()
