"""Geometry shared across service boundaries, without runtime dependencies."""

import math
from .model import Point, Polygon


def quadrilateral_center(polygon: Polygon) -> Point:
    """Intersection of diagonals, including perspective-shaped rectangles."""
    if len(polygon.points) != 4:
        raise ValueError("a quadrilateral needs exactly four vertices")
    a, b, c, d = polygon.points
    rx, ry = c.x - a.x, c.y - a.y
    sx, sy = d.x - b.x, d.y - b.y
    denominator = rx * sy - ry * sx
    if abs(denominator) < 1e-9:
        raise ValueError("quadrilateral diagonals do not intersect uniquely")
    qx, qy = b.x - a.x, b.y - a.y
    factor = (qx * sy - qy * sx) / denominator
    return Point(a.x + factor * rx, a.y + factor * ry)


def offset_convex_polygon(points, distance: float) -> list[tuple[float, float]] | None:
    """A convex polygon with every edge moved ``distance`` along its normal.

    Positive moves outward, negative inward, either winding; vertices are the
    miter intersections of neighboring moved edges. None when the polygon is
    degenerate (fewer than 3 points, zero area, a zero-length or parallel
    neighboring edge). Mirrors ``offsetConvexPolygon`` in the web UI without
    its miter limit: callers here only need where a rim lies, not its bevel.
    """
    pts = [(float(x), float(y)) for x, y in points]
    count = len(pts)
    if count < 3:
        return None
    area2 = 0.0
    for index in range(count):
        x1, y1 = pts[index]
        x2, y2 = pts[(index + 1) % count]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) < 1e-9:
        return None
    sign = 1.0 if area2 > 0 else -1.0
    lines = []
    for index in range(count):
        x, y = pts[index]
        dx, dy = pts[(index + 1) % count][0] - x, pts[(index + 1) % count][1] - y
        length = math.sqrt(dx * dx + dy * dy)   # sqrt, not ** 0.5: the JS port gets the same bits
        if length == 0:
            return None
        lines.append((x + sign * dy / length * distance, y - sign * dx / length * distance, dx, dy))
    result = []
    for index, (x, y, dx, dy) in enumerate(lines):
        px, py, pdx, pdy = lines[index - 1]
        denominator = pdx * dy - pdy * dx
        if denominator == 0:
            return None
        along = ((x - px) * dy - (y - py) * dx) / denominator
        result.append((px + along * pdx, py + along * pdy))
    return result
