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
SAMPLE_CAP = 256            # vertices sampled per part for the gap measurement
CONTACT_SAMPLE_CAP = 1024   # surface samples per part and direction for the touch test
MAX_PAIRS = 200_000         # candidate pairs above which the whole check is skipped
MAX_DETACHED = 20           # detached parts listed in a report
MAX_NODES = 4000            # islands across all parts; past this the parts with the most islands stay single nodes
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


class TooManyPairs(ValueError):
    """More candidate pairs than ``MAX_PAIRS``: the check is skipped rather than allowed to run away."""


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


def candidate_pairs(boxes, tol, limit=None) -> list:
    """Pairs ``(i, j)``, ``i < j``, whose boxes expanded by ``tol`` overlap: the only pairs worth a BVH test.

    ``boxes[i]`` is a box or ``None`` for a part that takes no part in the check.  Sweeps along X, so a
    prop of a few hundred parts costs far less than all pairs.  With ``limit``, raises ``TooManyPairs`` as soon as
    more pairs than that are found."""
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
                if limit is not None and len(pairs) > limit:
                    raise TooManyPairs("more than %d candidate pairs" % limit)
    pairs.sort()
    return pairs


def split_islands(triangles, limit=None):
    """Group triangle indices into loose islands (triangles that share a vertex, directly or through others).

    A part made of several separate pieces (a join, or a kit mesh with detached bits) is several nodes of the
    touching graph, otherwise one grounded piece would hide a floating one.  Returns lists of triangle indices
    ordered by their first triangle, or ``None`` when ``limit`` is given and there are more islands than that."""
    parent = {}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for tri in triangles:
        for vertex in tri:
            parent.setdefault(vertex, vertex)
        root = find(tri[0])
        for vertex in tri[1:]:
            other = find(vertex)
            if other != root:
                parent[max(root, other)] = min(root, other)
                root = min(root, other)
    groups = {}
    for index, tri in enumerate(triangles):
        groups.setdefault(find(tri[0]), []).append(index)
        if limit is not None and len(groups) > limit:
            return None
    return [groups[key] for key in sorted(groups, key=lambda key: groups[key][0])]


def single_node_parts(counts, budget=MAX_NODES) -> set:
    """Which parts stay a single node so that the total number of nodes fits ``budget``.

    ``counts[i]`` is the number of islands of part ``i``.  Every part is split while the budget allows; once it is
    exceeded the parts with the most islands (the likeliest to be unwelded junk) are collapsed first, lowest index
    first among equals, until the total fits.  Returns the indices to collapse."""
    total = sum(max(1, count) for count in counts)
    collapsed = set()
    for index in sorted(range(len(counts)), key=lambda i: (-counts[i], i)):
        if total <= budget:
            break
        if counts[index] > 1:
            total -= counts[index] - 1
            collapsed.add(index)
    return collapsed


def is_closed_manifold(triangles) -> bool:
    """True when every edge is shared by exactly two triangles (a watertight surface, so "inside" means something)."""
    edges = {}
    for tri in triangles:
        for index in range(3):
            a, b = tri[index], tri[(index + 1) % 3]
            key = (a, b) if a < b else (b, a)
            edges[key] = edges.get(key, 0) + 1
    return bool(edges) and all(count == 2 for count in edges.values())


def boxes_near(a, b, tol) -> bool:
    """True when two boxes overlap or lie within ``tol`` of each other."""
    return box_distance(a, b) <= tol


def box_contains(outer, inner) -> bool:
    """True when the box ``inner`` lies entirely inside the box ``outer``."""
    return all(outer[axis] <= inner[axis] and inner[axis + 3] <= outer[axis + 3] for axis in range(3))


def _clip_plane(polygon, axis, bound, keep_above):
    out = []
    for index, current in enumerate(polygon):
        following = polygon[(index + 1) % len(polygon)]
        inside = current[axis] >= bound if keep_above else current[axis] <= bound
        next_inside = following[axis] >= bound if keep_above else following[axis] <= bound
        if inside:
            out.append(current)
        if inside != next_inside:
            t = (bound - current[axis]) / (following[axis] - current[axis])
            out.append(tuple(current[k] + t * (following[k] - current[k]) for k in range(3)))
    return out


def clip_polygon(polygon, box) -> list:
    """The part of a convex polygon (list of 3-tuples) inside an axis-aligned box (Sutherland-Hodgman); ``[]`` if none."""
    for axis in range(3):
        for keep_above, bound in ((True, box[axis]), (False, box[axis + 3])):
            polygon = _clip_plane(polygon, axis, bound, keep_above)
            if not polygon:
                return []
    return polygon


def _mean(points):
    count = float(len(points))
    return tuple(sum(point[k] for point in points) / count for k in range(3))


def triangle_boxes(points, triangles) -> list:
    """Axis-aligned box of every triangle (``points`` are 3-tuples, ``triangles`` index triples)."""
    boxes = []
    for tri in triangles:
        corners = [points[i] for i in tri]
        boxes.append(tuple(min(c[k] for c in corners) for k in range(3)) + tuple(max(c[k] for c in corners)
                                                                                for k in range(3)))
    return boxes


def contact_samples(points, triangles, box, tol, cap=CONTACT_SAMPLE_CAP, tri_boxes=None) -> list:
    """Points on a part's surface that could lie within ``tol`` of something inside ``box`` (the other part's box).

    Anything within ``tol`` of that part lies inside ``box`` grown by ``tol``, so each triangle is clipped to the
    grown box and sampled where it matters: the clipped polygon's corners, its edge midpoints, its centroid and, for
    polygons with four or more corners, the centroids of a fan of triangles.  A flush face, a line contact or a
    contact patch smaller than the part's own vertex spacing therefore still gets samples in the contact region.
    Triangles nearest the box centre come first and at most ``cap`` points are returned."""
    region = tuple(box[k] - tol if k < 3 else box[k] + tol for k in range(6))
    centre = [(box[k] + box[k + 3]) / 2.0 for k in range(3)]
    if tri_boxes is None:
        tri_boxes = triangle_boxes(points, triangles)
    ranked = []
    for index, tb in enumerate(tri_boxes):
        if all(tb[k] <= region[k + 3] and region[k] <= tb[k + 3] for k in range(3)):
            ranked.append((sum(((tb[k] + tb[k + 3]) / 2.0 - centre[k]) ** 2 for k in range(3)), index))
    ranked.sort()
    samples = []
    for _distance, index in ranked:
        polygon = clip_polygon([points[i] for i in triangles[index]], region)
        if not polygon:
            continue
        samples.extend(polygon)
        samples.extend(_mean([polygon[i], polygon[(i + 1) % len(polygon)]]) for i in range(len(polygon)))
        samples.append(_mean(polygon))
        if len(polygon) >= 4:
            samples.extend(_mean([polygon[0], polygon[i], polygon[i + 1]]) for i in range(1, len(polygon) - 1))
        if len(samples) >= cap:
            break
    return samples[:cap]


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


def _valid_box(box):
    return (isinstance(box, (list, tuple)) and len(box) == 6
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in box))


def floating_entry(names, lines, centre, gap, gap_vec, nearest, box=None) -> dict:
    """One report entry for a floating component.

    ``names``/``lines``: its parts and the script lines that created them (0 = unknown).  ``centre``: the
    world-space centre of its bounding box.  ``gap``: the distance to the nearest other part (or to the
    ground).  ``gap_vec``: from the nearest point of that other part to the closest point of the floating
    component, so a positive Z means it floats above.  ``nearest``: that other part's name, or ``None`` for
    the ground."""
    known = sorted({int(line) for line in lines if isinstance(line, int) and line > 0})
    entry = {
        "parts": [str(name)[:MAX_NAME_CHARS] for name in list(names)[:MAX_NAMES]],
        "part_count": len(names),
        "lines": known[:MAX_NAMES],
        "centre": _rounded(centre),
        "gap": _rounded([gap])[0],
        "gap_vec": _rounded(gap_vec),
        "nearest": None if nearest is None else str(nearest)[:MAX_NAME_CHARS],
    }
    if _valid_box(box):
        entry["box"] = _rounded(box)
    return entry


def geometry_block(part_count, entries, height, dims, detached=()) -> dict:
    """The ``geometry`` object of the harness result (at most ``MAX_FLOATING`` entries).

    ``detached`` lists ``(name, line, box)`` of the parts the script marked ``detached(...)`` (``box`` optional), so
    the loop can show the model, and the report, which parts were excluded from the check."""
    listed = []
    for item in list(detached)[:MAX_DETACHED]:
        name, line = item[0], item[1]
        record = {"name": str(name)[:MAX_NAME_CHARS], "line": int(line) if isinstance(line, int) else 0}
        if len(item) > 2 and _valid_box(item[2]):
            record["box"] = _rounded(item[2])
        listed.append(record)
    return {"parts": int(part_count), "floating": list(entries)[:MAX_FLOATING],
            "height": _rounded([height])[0], "dims": _rounded(dims), "detached": listed}


__all__ = ["CONTACT_SAMPLE_CAP", "MAX_DETACHED", "MAX_FLOATING", "MAX_NODES", "MAX_NAMES", "MAX_NAME_CHARS",
           "MAX_PAIRS", "boxes_near", "is_closed_manifold", "single_node_parts", "split_islands",
           "SAMPLE_CAP", "TOL_FLOOR", "TOL_FRACTION", "TooManyPairs", "box_contains", "box_distance",
           "candidate_pairs", "clip_polygon", "contact_samples", "floating_components", "floating_entry",
           "geometry_block", "is_grounded", "sample_indices", "tolerance", "triangle_boxes"]
