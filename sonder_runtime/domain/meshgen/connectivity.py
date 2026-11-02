"""Part-connectivity maths for the floating-part check.  Pure stdlib, shipped into Blender.

The harness (``adapters/meshgen/harness.py``) measures which parts touch with Blender's BVH trees and
hands the answer to the functions here, which hold everything that does not need Blender: the
tolerance, the sampling stride, the bounding-box pruning, the union-find over the touching graph and the
shape of the report entries.  Like ``dsl/opschema.py`` this file is copied next to the harness, so it must
not import anything from the package.

Vocabulary: a *part* is one mesh object of the build, numbered ``0..count-1``.  Two parts *touch* when their
surfaces overlap or come within the tolerance.  A part is *grounded* when its lowest point is within the
tolerance of ``z = 0``.  A *component* of touching parts with no grounded part is *floating*.  A part marked
``detached(...)`` in the script is intentionally free and is left out of the graph altogether.
"""
from __future__ import annotations

import math

TOL_FLOOR = 0.01            # metres
TOL_FRACTION = 0.015        # of the overall height
SAMPLE_CAP = 256            # vertices sampled per part for the nearest-distance test
MAX_FLOATING = 20           # floating entries kept in a report
MAX_NAMES = 8               # part names / lines kept per entry
MAX_NAME_CHARS = 40


def tolerance(height) -> float:
    """``max(0.01 m, 1.5% of the overall height)``; a missing or invalid height gives the floor."""
    try:
        height = float(height)
    except (TypeError, ValueError):
        return TOL_FLOOR
    if not math.isfinite(height) or height < 0:
        return TOL_FLOOR
    return max(TOL_FLOOR, TOL_FRACTION * height)


def is_grounded(min_z, tol) -> bool:
    """A part stands on the ground when its lowest point is at most ``tol`` above ``z = 0``."""
    return min_z <= tol


def sample_indices(count, cap=SAMPLE_CAP) -> list:
    """At most ``cap`` indices spread evenly over ``range(count)``; all of them when ``count <= cap``."""
    if count <= 0 or cap <= 0:
        return []
    if count <= cap:
        return list(range(count))
    return [(i * count) // cap for i in range(cap)]


def box_distance(a, b) -> float:
    """Lower bound of the distance between two parts: the gap between their axis-aligned boxes (0 if they
    overlap).  A box is ``(min_x, min_y, min_z, max_x, max_y, max_z)``."""
    total = 0.0
    for axis in range(3):
        gap = max(a[axis] - b[axis + 3], b[axis] - a[axis + 3], 0.0)
        total += gap * gap
    return math.sqrt(total)


def candidate_pairs(boxes, tol) -> list:
    """Pairs ``(i, j)``, ``i < j``, whose boxes expanded by ``tol`` overlap: the only pairs worth a BVH test.

    ``boxes[i]`` is a box or ``None`` for a part that takes no part in the check.  Sweeps along X, so a
    prop of a few hundred parts costs far less than all pairs."""
    order = sorted((i for i, box in enumerate(boxes) if box is not None), key=lambda i: boxes[i][0])
    pairs = []
    for position, i in enumerate(order):
        a = boxes[i]
        for j in order[position + 1:]:
            b = boxes[j]
            if b[0] > a[3] + tol:
                break
            if b[1] <= a[4] + tol and a[1] <= b[4] + tol and b[2] <= a[5] + tol and a[2] <= b[5] + tol:
                pairs.append((min(i, j), max(i, j)))
    pairs.sort()
    return pairs


def floating_components(count, touching, grounded, detached) -> list:
    """Components of the touching graph that no grounded part belongs to.

    ``touching`` is an iterable of ``(i, j)``; ``grounded`` and ``detached`` are sequences of ``count``
    booleans.  A detached part is not a node: it neither floats, nor grounds, nor connects its neighbours.
    Returns lists of part indices (each sorted), ordered by their first member."""
    parent = list(range(count))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for i, j in touching:
        if 0 <= i < count and 0 <= j < count and not detached[i] and not detached[j]:
            root_i, root_j = find(i), find(j)
            if root_i != root_j:
                parent[max(root_i, root_j)] = min(root_i, root_j)
    members = {}
    for item in range(count):
        if not detached[item]:
            members.setdefault(find(item), []).append(item)
    return [group for _root, group in sorted(members.items())
            if not any(grounded[item] for item in group)]


def _rounded(values, digits=4) -> list:
    return [round(float(value), digits) if math.isfinite(float(value)) else 0.0 for value in values]


def floating_entry(names, lines, centre, gap, gap_vec, nearest) -> dict:
    """One report entry for a floating component.

    ``names``/``lines``: its parts and the script lines that created them (0 = unknown).  ``centre``: the
    world-space centre of its bounding box.  ``gap``: the distance to the nearest other part (or to the
    ground).  ``gap_vec``: from the nearest point of that other part to the closest point of the floating
    component, so a positive Z means it floats above.  ``nearest``: that other part's name, or ``None`` for
    the ground."""
    known = sorted({int(line) for line in lines if isinstance(line, int) and line > 0})
    return {
        "parts": [str(name)[:MAX_NAME_CHARS] for name in list(names)[:MAX_NAMES]],
        "part_count": len(names),
        "lines": known[:MAX_NAMES],
        "centre": _rounded(centre),
        "gap": _rounded([gap])[0],
        "gap_vec": _rounded(gap_vec),
        "nearest": None if nearest is None else str(nearest)[:MAX_NAME_CHARS],
    }


def geometry_block(part_count, entries, height, dims) -> dict:
    """The ``geometry`` object of the harness result (at most ``MAX_FLOATING`` entries)."""
    return {"parts": int(part_count), "floating": list(entries)[:MAX_FLOATING],
            "height": _rounded([height])[0], "dims": _rounded(dims)}


__all__ = ["MAX_FLOATING", "MAX_NAMES", "MAX_NAME_CHARS", "SAMPLE_CAP", "TOL_FLOOR", "TOL_FRACTION", "box_distance",
           "candidate_pairs", "floating_components", "floating_entry", "geometry_block", "is_grounded",
           "sample_indices", "tolerance"]
