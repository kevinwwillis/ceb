"""Arrangement geometry for the canvas: snapping while dragging, and making a dropped layout valid.

Pure Python (no GTK) so it can be tested on its own. Units are layout pixels, the same units as the
x/y of a profile's logical monitors.

GNOME (Mutter) refuses a layout whose screens overlap, or where a screen does not touch another
screen along an edge ("Logical monitors not adjacent"). Corners touching is not enough.
"""
from __future__ import annotations

from dataclasses import dataclass

SNAP_DISTANCE = 120  # layout pixels; the same distance as the picked mockup


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def moved(self, x: int, y: int) -> "Rect":
        return Rect(x, y, self.w, self.h)


def overlaps(a: Rect, b: Rect) -> bool:
    return a.x < b.x2 and b.x < a.x2 and a.y < b.y2 and b.y < a.y2


def adjacent(a: Rect, b: Rect) -> bool:
    """Mutter's rule: the rectangles share part of an edge (positive length), without overlapping."""
    if (a.x == b.x2 or a.x2 == b.x) and a.y < b.y2 and b.y < a.y2:
        return True
    return (a.y == b.y2 or a.y2 == b.y) and a.x < b.x2 and b.x < a.x2


def is_valid(rects: list[Rect]) -> bool:
    """No two overlap, and every screen is reachable from every other through shared edges."""
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            if overlaps(a, b):
                return False
    if len(rects) <= 1:
        return True
    seen = {0}
    todo = [0]
    while todo:
        i = todo.pop()
        for j, b in enumerate(rects):
            if j not in seen and adjacent(rects[i], b):
                seen.add(j)
                todo.append(j)
    return len(seen) == len(rects)


def snap(x: float, y: float, w: int, h: int, others: list[Rect], distance: int = SNAP_DISTANCE) -> tuple[int, int]:
    """Snap a screen being dragged to its neighbours' edges. Horizontally: its left edge to a
    neighbour's right or left edge, its right edge to a neighbour's left or right edge (side by side,
    or aligned left or right). Vertically the same with top and bottom. On each axis the nearest
    edge pair within `distance` wins; the mockup also had the first four pairs, but applied every
    pair in turn, and had no right/right or bottom/bottom alignment."""
    def nearest(pos: float, size: int, pairs) -> float:
        best = None
        for a, b in pairs:
            d = b - a
            if abs(d) < distance and (best is None or abs(d) < abs(best)):
                best = d
        return pos + (best or 0)

    x = nearest(x, w, [(a, b) for o in others
                       for a, b in ((x, o.x2), (x + w, o.x), (x, o.x), (x + w, o.x2))])
    y = nearest(y, h, [(a, b) for o in others
                       for a, b in ((y, o.y), (y, o.y2), (y + h, o.y), (y + h, o.y2))])
    return round(x), round(y)


NUDGE_STEP = 100     # layout pixels per step when moving a screen with the keyboard
NUDGE_MAX_STEPS = 200


def nudge(rects: list[Rect], index: int, dx: int, dy: int, step: int = NUDGE_STEP) -> list[Rect] | None:
    """Move rects[index] one step in direction (dx, dy) (each -1, 0 or 1) as a drag would: the
    first position in that direction, `step` by `step`, that after snapping still moves that way
    and gives a layout GNOME accepts. None when there is no such place (e.g. moving a screen away
    from the only screen it touches)."""
    r = rects[index]
    others = [o for i, o in enumerate(rects) if i != index]
    for k in range(1, NUDGE_MAX_STEPS + 1):
        x, y = snap(r.x + dx * k * step, r.y + dy * k * step, r.w, r.h, others)
        if (x - r.x) * dx <= 0 and dx or (y - r.y) * dy <= 0 and dy:
            continue  # snapped back or sideways only
        trial = rects[:index] + [r.moved(x, y)] + rects[index + 1:]
        if is_valid(trial):
            return trial
    return None


def settle(rects: list[Rect], index: int, fallback: Rect) -> tuple[list[Rect], bool]:
    """Make the layout valid after rects[index] was dropped where it is.

    If it is already valid, it is kept. Otherwise the dropped screen moves to the nearest place
    where it touches a neighbour along an edge and overlaps nothing (as GNOME Settings does). If no
    such place keeps the whole layout valid, it goes back to `fallback` (where the drag started).
    Returns the new rectangles and whether the dropped screen ended where it was dropped."""
    if is_valid(rects):
        return rects, True
    moved = rects[index]
    others = [r for i, r in enumerate(rects) if i != index]
    good: tuple[float, Rect] | None = None   # shares a real stretch of edge with a neighbour
    thin: tuple[float, Rect] | None = None   # touches by a sliver only (valid, but looks wrong)
    for o in others:
        for cand in _touching_positions(moved, o):
            trial = rects[:index] + [cand] + rects[index + 1:]
            if not is_valid(trial):
                continue
            dist = (cand.x - moved.x) ** 2 + (cand.y - moved.y) ** 2
            if max(shared_edge(cand, n) for n in others) >= min(cand.w, cand.h) / 4:
                if good is None or dist < good[0]:
                    good = (dist, cand)
            elif thin is None or dist < thin[0]:
                thin = (dist, cand)
    best = good or thin
    if best is not None:
        return rects[:index] + [best[1]] + rects[index + 1:], False
    return rects[:index] + [fallback] + rects[index + 1:], False


def shared_edge(a: Rect, b: Rect) -> int:
    """Length of the edge two adjacent rectangles share (0 when they are not adjacent)."""
    if not adjacent(a, b):
        return 0
    if a.x == b.x2 or a.x2 == b.x:
        return min(a.y2, b.y2) - max(a.y, b.y)
    return min(a.x2, b.x2) - max(a.x, b.x)


def _touching_positions(r: Rect, o: Rect) -> list[Rect]:
    """Places for r that touch o along an edge, as close as possible to where r is now."""
    y = min(max(r.y, o.y - r.h + 1), o.y2 - 1)  # keep at least 1 px of shared edge
    x = min(max(r.x, o.x - r.w + 1), o.x2 - 1)
    cands = [r.moved(o.x - r.w, y), r.moved(o.x2, y), r.moved(x, o.y - r.h), r.moved(x, o.y2)]
    # Also the aligned versions (top-aligned / left-aligned), which is what people usually want.
    cands += [r.moved(o.x - r.w, o.y), r.moved(o.x2, o.y), r.moved(o.x, o.y - r.h), r.moved(o.x, o.y2)]
    return cands


def origin_shift(rects: list[Rect]) -> tuple[int, int]:
    """How far to move every screen so the layout starts at (0, 0), as Mutter expects."""
    if not rects:
        return 0, 0
    return -min(r.x for r in rects), -min(r.y for r in rects)


def close_gap(rects: list[Rect], removed: Rect) -> list[Rect]:
    """After a screen is taken out of a layout, slide the screens that were to its right (or below
    it) back by its width (height) when that makes the layout valid again. Otherwise unchanged."""
    if is_valid(rects):
        return rects
    shifted = [r.moved(r.x - removed.w, r.y) if r.x >= removed.x2 else r for r in rects]
    if is_valid(shifted):
        return shifted
    shifted = [r.moved(r.x, r.y - removed.h) if r.y >= removed.y2 else r for r in rects]
    if is_valid(shifted):
        return shifted
    return rects
