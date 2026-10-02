"""Geometry shared across service boundaries, without runtime dependencies."""

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
