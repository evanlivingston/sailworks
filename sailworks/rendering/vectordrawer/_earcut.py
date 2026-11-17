"""Pure-Python ear-clipping triangulation (vendored, no dependencies).

Provenance
----------
A direct port of ``mapbox/earcut`` 2.2.4, the reference JavaScript
implementation of the ear-clipping algorithm with hole elimination and
z-order hashing:

    https://github.com/mapbox/earcut

    Copyright (c) 2016, Mapbox
    ISC licence

    Permission to use, copy, modify, and/or distribute this software for any
    purpose with or without fee is hereby granted, provided that the above
    copyright notice and this permission notice appear in all copies.

    THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
    WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
    MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
    ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
    WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
    ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
    OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

The only changes are mechanical: JavaScript objects became ``_Node``
instances, ``| 0`` truncation became ``int()``, lattice bit-twiddling keeps
the same 32-bit masks, and functions are renamed with a leading underscore.
Comments that explain a non-obvious invariant were kept so a future reader
can compare against the reference.

Contract: :func:`earcut` takes flat ``[x0, y0, x1, y1, ...]`` coordinates,
hole start indices counted in *vertices* (not floats), and a dimension
(default 2).  It returns a flat list of vertex indices, three per triangle.
Outer rings may be given in either orientation; holes are bridged into the
outer ring.  Degenerate input (fewer than three distinct vertices) returns an
empty list rather than raising; callers reconcile the returned area.
"""

from __future__ import annotations

__all__ = ["earcut"]

_FLAT = 0x00FF00FF
_WORD_MASK = 0x0F0F0F0F
_PAIR_MASK = 0x33333333
_NIBBLE_MASK = 0x55555555


class _Node:
    """Doubly linked list node, also a member of the z-order sorted list."""

    __slots__ = ("i", "x", "y", "prev", "next", "z", "prev_z", "next_z", "steiner")

    def __init__(self, i: int, x: float, y: float) -> None:
        self.i = i
        self.x = x
        self.y = y
        self.prev = None
        self.next = None
        self.z = 0
        self.prev_z = None
        self.next_z = None
        self.steiner = False


def _insert_node(i: int, x: float, y: float, last):
    p = _Node(i, x, y)
    if last is None:
        p.prev = p
        p.next = p
    else:
        p.next = last.next
        p.prev = last
        last.next.prev = p
        last.next = p
    return p


def _remove_node(p) -> None:
    p.next.prev = p.prev
    p.prev.next = p.next
    if p.prev_z is not None:
        p.prev_z.next_z = p.next_z
    if p.next_z is not None:
        p.next_z.prev_z = p.prev_z


def _filter_points(start, end=None):
    """Drop duplicate and collinear points (and all points if all collinear)."""
    if start is None:
        return start
    if end is None:
        end = start
    p = start
    again = True
    while again or p is not end:
        again = False
        if not p.steiner and (_equals(p, p.next) or _area(p.prev, p, p.next) == 0):
            _remove_node(p)
            p = end = p.prev
            if p is p.next:
                break
            again = True
        else:
            p = p.next
    return end


def _earcut_linked(ear, triangles, dim, min_x, min_y, inv_size, pass_number=0):
    if ear is None:
        return
    # Interlink the z-order sorted list on the first pass only.
    if not pass_number and inv_size:
        ear = _index_curve(ear, min_x, min_y, inv_size)

    stop = ear
    while ear.prev is not ear.next:
        prev = ear.prev
        nxt = ear.next
        if _is_ear_hashed(ear, min_x, min_y, inv_size) if inv_size else _is_ear(ear):
            triangles.append(prev.i // dim)
            triangles.append(ear.i // dim)
            triangles.append(nxt.i // dim)
            _remove_node(ear)
            ear = nxt.next
            stop = nxt.next
            continue

        ear = nxt
        if ear is stop:
            # No ear found: the polygon is self-intersecting or otherwise
            # unusable, so try progressively more aggressive repairs.
            if not pass_number:
                _earcut_linked(
                    _filter_points(ear), triangles, dim, min_x, min_y, inv_size, 1
                )
            elif pass_number == 1:
                ear = _cure_local_intersections(_filter_points(ear), triangles, dim)
                _earcut_linked(ear, triangles, dim, min_x, min_y, inv_size, 2)
            elif pass_number == 2:
                _split_earcut(ear, triangles, dim, min_x, min_y, inv_size)
            break


def _is_ear(ear) -> bool:
    a = ear.prev
    b = ear
    c = ear.next
    if _area(a, b, c) >= 0:
        return False  # reflex or degenerate, can't be an ear

    ax, ay = a.x, a.y
    bx, by = b.x, b.y
    cx, cy = c.x, c.y
    x0 = ax if ax < bx else (ax if ax < cx else cx)
    y0 = ay if ay < by else (ay if ay < cy else cy)
    x1 = ax if ax > bx else (ax if ax > cx else cx)
    y1 = ay if ay > by else (ay if ay > cy else cy)

    p = c.next
    while p is not a:
        if (
            x0 <= p.x <= x1
            and y0 <= p.y <= y1
            and _point_in_triangle(ax, ay, bx, by, cx, cy, p.x, p.y)
            and _area(p.prev, p, p.next) >= 0
        ):
            return False
        p = p.next
    return True


def _is_ear_hashed(ear, min_x, min_y, inv_size) -> bool:
    a = ear.prev
    b = ear
    c = ear.next
    if _area(a, b, c) >= 0:
        return False

    ax, ay = a.x, a.y
    bx, by = b.x, b.y
    cx, cy = c.x, c.y
    x0 = ax if ax < bx else (ax if ax < cx else cx)
    y0 = ay if ay < by else (ay if ay < cy else cy)
    x1 = ax if ax > bx else (ax if ax > cx else cx)
    y1 = ay if ay > by else (ay if ay > cy else cy)

    min_z = _z_order(x0, y0, min_x, min_y, inv_size)
    max_z = _z_order(x1, y1, min_x, min_y, inv_size)

    p = ear.prev_z
    n = ear.next_z
    while p is not None and p.z >= min_z and n is not None and n.z <= max_z:
        if (
            p is not ear.prev
            and p is not ear.next
            and _point_in_triangle(ax, ay, bx, by, cx, cy, p.x, p.y)
            and _area(p.prev, p, p.next) >= 0
        ):
            return False
        p = p.prev_z
        if (
            n is not ear.prev
            and n is not ear.next
            and _point_in_triangle(ax, ay, bx, by, cx, cy, n.x, n.y)
            and _area(n.prev, n, n.next) >= 0
        ):
            return False
        n = n.next_z
    while p is not None and p.z >= min_z:
        if (
            p is not ear.prev
            and p is not ear.next
            and _point_in_triangle(ax, ay, bx, by, cx, cy, p.x, p.y)
            and _area(p.prev, p, p.next) >= 0
        ):
            return False
        p = p.prev_z
    while n is not None and n.z <= max_z:
        if (
            n is not ear.prev
            and n is not ear.next
            and _point_in_triangle(ax, ay, bx, by, cx, cy, n.x, n.y)
            and _area(n.prev, n, n.next) >= 0
        ):
            return False
        n = n.next_z
    return True


def _cure_local_intersections(start, triangles, dim):
    p = start
    while True:
        a = p.prev
        b = p.next.next
        if (
            not _equals(a, b)
            and _intersects(a, p, p.next, b)
            and _locally_inside(a, b)
            and _locally_inside(b, a)
        ):
            triangles.append(a.i // dim)
            triangles.append(p.i // dim)
            triangles.append(b.i // dim)
            _remove_node(p)
            _remove_node(p.next)
            p = start = b
        p = p.next
        if p is start:
            break
    return _filter_points(p)


def _split_earcut(start, triangles, dim, min_x, min_y, inv_size):
    a = start
    while True:
        b = a.next.next
        while b is not a.prev:
            if a.i != b.i and _is_valid_diagonal(a, b):
                c = _split_polygon(a, b)
                a = _filter_points(a, a.next)
                c = _filter_points(c, c.next)
                _earcut_linked(a, triangles, dim, min_x, min_y, inv_size, 0)
                _earcut_linked(c, triangles, dim, min_x, min_y, inv_size, 0)
                return
            b = b.next
        a = a.next
        if a is start:
            break


def _eliminate_holes(data, hole_indices, outer_node, dim):
    queue = []
    length = len(hole_indices)
    for i in range(length):
        start = hole_indices[i] * dim
        end = hole_indices[i + 1] * dim if i < length - 1 else len(data)
        ring = _linked_list(data, start, end, dim, False)
        if ring is ring.next:
            ring.steiner = True
        queue.append(_get_leftmost(ring))
    queue.sort(key=lambda node: node.x)
    for hole in queue:
        outer_node = _eliminate_hole(hole, outer_node)
    return outer_node


def _eliminate_hole(hole, outer_node):
    bridge = _find_hole_bridge(hole, outer_node)
    if bridge is None:
        return outer_node
    bridge_reverse = _split_polygon(bridge, hole)
    _filter_points(bridge_reverse, bridge_reverse.next)
    return _filter_points(bridge, bridge.next)


def _find_hole_bridge(hole, outer_node):
    p = outer_node
    hx = hole.x
    hy = hole.y
    qx = float("-inf")
    m = None

    # Find a segment intersected by a ray from the hole's leftmost point to the
    # left; segment's endpoint with lesser x will be the potential connection.
    while True:
        if hy <= p.y and hy >= p.next.y and p.next.y != p.y:
            x = p.x + (hy - p.y) * (p.next.x - p.x) / (p.next.y - p.y)
            if x <= hx and x > qx:
                qx = x
                if p.x < p.next.x:
                    m = p
                else:
                    m = p.next
                if x == hx:
                    return m  # hole touches the outer edge exactly
        p = p.next
        if p is outer_node:
            break

    if m is None:
        return None

    # Look for points inside the triangle formed by the hole leftmost point and
    # the visible outer node, choosing the one with the lowest tangent angle.
    stop = m
    mx = m.x
    my = m.y
    tan_min = float("inf")
    p = m
    while True:
        if (
            hx >= p.x
            and p.x >= mx
            and hx != p.x
            and _point_in_triangle(
                hx if hy < my else qx,
                hy,
                mx,
                my,
                qx if hy < my else hx,
                hy,
                p.x,
                p.y,
            )
        ):
            tan = abs(hy - p.y) / (hx - p.x)
            if _locally_inside(p, hole) and (
                tan < tan_min
                or (
                    tan == tan_min
                    and (p.x > m.x or (p.x == m.x and _sector_contains_sector(m, p)))
                )
            ):
                m = p
                tan_min = tan
        p = p.next
        if p is stop:
            break
    return m


def _sector_contains_sector(m, p) -> bool:
    return _area(m.prev, m, p.prev) < 0 and _area(p.next, m, m.next) < 0


def _index_curve(start, min_x, min_y, inv_size):
    p = start
    while True:
        if p.z == 0:
            p.z = _z_order(p.x, p.y, min_x, min_y, inv_size)
        p.prev_z = p.prev
        p.next_z = p.next
        p = p.next
        if p is start:
            break
    p.prev_z.next_z = None
    p.prev_z = None
    return _sort_linked(p)


def _sort_linked(hook):
    """Merge-sort a linked list by ``z`` (Simon Tatham's algorithm)."""
    in_size = 1
    while True:
        p = hook
        hook = None
        tail = None
        num_merges = 0
        while p is not None:
            num_merges += 1
            q = p
            p_size = 0
            for _ in range(in_size):
                p_size += 1
                q = q.next_z
                if q is None:
                    break
            q_size = in_size

            while p_size > 0 or (q_size > 0 and q is not None):
                if p_size != 0 and (q_size == 0 or q is None or p.z <= q.z):
                    e = p
                    p = p.next_z
                    p_size -= 1
                else:
                    e = q
                    q = q.next_z
                    q_size -= 1
                if tail is not None:
                    tail.next_z = e
                else:
                    hook = e
                e.prev_z = tail
                tail = e
            p = q
        if tail is not None:
            tail.next_z = None
        in_size *= 2
        if num_merges <= 1:
            break
    return hook


def _get_leftmost(start):
    p = start
    leftmost = start
    while True:
        if p.x < leftmost.x or (p.x == leftmost.x and p.y < leftmost.y):
            leftmost = p
        p = p.next
        if p is start:
            break
    return leftmost


def _point_in_triangle(ax, ay, bx, by, cx, cy, px, py) -> bool:
    return (
        (cx - px) * (ay - py) >= (ax - px) * (cy - py)
        and (ax - px) * (by - py) >= (bx - px) * (ay - py)
        and (bx - px) * (cy - py) >= (cx - px) * (by - py)
    )


def _is_valid_diagonal(a, b) -> bool:
    return (
        a.next.i != b.i
        and a.prev.i != b.i
        and not _intersects_polygon(a, b)
        and (
            (
                _locally_inside(a, b)
                and _locally_inside(b, a)
                and _middle_inside(a, b)
                and (_area(a.prev, a, b.prev) != 0 or _area(a, b.prev, b) != 0)
            )
            or (
                _equals(a, b)
                and _area(a.prev, a, a.next) > 0
                and _area(b.prev, b, b.next) > 0
            )
        )
    )


def _intersects_polygon(a, b) -> bool:
    p = a
    while True:
        if (
            p.i != a.i
            and p.next.i != a.i
            and p.i != b.i
            and p.next.i != b.i
            and _intersects(p, p.next, a, b)
        ):
            return True
        p = p.next
        if p is a:
            break
    return False


def _locally_inside(a, b) -> bool:
    if _area(a.prev, a, a.next) < 0:
        return _area(a, b, a.next) >= 0 and _area(a, a.prev, b) >= 0
    return _area(a, b, a.prev) < 0 or _area(a, a.next, b) < 0


def _middle_inside(a, b) -> bool:
    p = a
    inside = False
    px = (a.x + b.x) / 2
    py = (a.y + b.y) / 2
    while True:
        if (
            (p.y > py) != (p.next.y > py)
            and p.next.y != p.y
            and px < (p.next.x - p.x) * (py - p.y) / (p.next.y - p.y) + p.x
        ):
            inside = not inside
        p = p.next
        if p is a:
            break
    return inside


def _split_polygon(a, b):
    a2 = _Node(a.i, a.x, a.y)
    b2 = _Node(b.i, b.x, b.y)
    an = a.next
    bp = b.prev

    a.next = b
    b.prev = a

    a2.next = an
    an.prev = a2

    b2.next = a2
    a2.prev = b2

    bp.next = b2
    b2.prev = bp

    return b2


def _linked_list(data, start, end, dim, clockwise):
    last = None
    if clockwise == (_signed_area(data, start, end, dim) > 0):
        i = start
        while i < end:
            last = _insert_node(i, data[i], data[i + 1], last)
            i += dim
    else:
        i = end - dim
        while i >= start:
            last = _insert_node(i, data[i], data[i + 1], last)
            i -= dim
    if last is not None and _equals(last, last.next):
        _remove_node(last)
        last = last.next
    return last


def _z_order(x, y, min_x, min_y, inv_size) -> int:
    x = int((x - min_x) * inv_size)
    y = int((y - min_y) * inv_size)

    x = (x | (x << 8)) & _FLAT
    x = (x | (x << 4)) & _WORD_MASK
    x = (x | (x << 2)) & _PAIR_MASK
    x = (x | (x << 1)) & _NIBBLE_MASK

    y = (y | (y << 8)) & _FLAT
    y = (y | (y << 4)) & _WORD_MASK
    y = (y | (y << 2)) & _PAIR_MASK
    y = (y | (y << 1)) & _NIBBLE_MASK

    return x | (y << 1)


def _signed_area(data, start, end, dim) -> float:
    total = 0.0
    i = start
    j = end - dim
    while i < end:
        total += (data[j] - data[i]) * (data[i + 1] + data[j + 1])
        j = i
        i += dim
    return total


def _area(p, q, r) -> float:
    return (q.y - p.y) * (r.x - q.x) - (q.x - p.x) * (r.y - q.y)


def _equals(p1, p2) -> bool:
    return p1.x == p2.x and p1.y == p2.y


def _intersects(p1, q1, p2, q2) -> bool:
    o1 = _sign(_area(p1, q1, p2))
    o2 = _sign(_area(p1, q1, q2))
    o3 = _sign(_area(p2, q2, p1))
    o4 = _sign(_area(p2, q2, q1))

    if o1 != o2 and o3 != o4:
        return True  # general case

    # Special cases: collinear and overlapping segments.
    if o1 == 0 and _on_segment(p1, p2, q1):
        return True
    if o2 == 0 and _on_segment(p1, q2, q1):
        return True
    if o3 == 0 and _on_segment(p2, p1, q2):
        return True
    if o4 == 0 and _on_segment(p2, q1, q2):
        return True
    return False


def _on_segment(p, q, r) -> bool:
    return (
        q.x <= max(p.x, r.x)
        and q.x >= min(p.x, r.x)
        and q.y <= max(p.y, r.y)
        and q.y >= min(p.y, r.y)
    )


def _sign(value) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def earcut(data, hole_indices=None, dim: int = 2):
    """Triangulate flat coordinates, returning vertex indices (three per face)."""
    has_holes = bool(hole_indices)
    outer_len = hole_indices[0] * dim if has_holes else len(data)
    outer_node = _linked_list(data, 0, outer_len, dim, True)
    triangles = []

    if outer_node is None or outer_node.next is outer_node.prev:
        return triangles

    min_x = min_y = None
    inv_size = 0.0
    if has_holes:
        outer_node = _eliminate_holes(data, hole_indices, outer_node, dim)

    # Large polygons get the z-order hash; small ones are cheaper without it.
    if len(data) > 80 * dim:
        min_x = max_x = data[0]
        min_y = max_y = data[1]
        i = dim
        while i < outer_len:
            x = data[i]
            y = data[i + 1]
            if x < min_x:
                min_x = x
            if y < min_y:
                min_y = y
            if x > max_x:
                max_x = x
            if y > max_y:
                max_y = y
            i += dim
        inv_size = max(max_x - min_x, max_y - min_y)
        inv_size = 1.0 / inv_size if inv_size != 0 else 0.0

    _earcut_linked(outer_node, triangles, dim, min_x, min_y, inv_size)
    return triangles
