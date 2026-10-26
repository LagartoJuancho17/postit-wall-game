"""One-parameter radial lens model for the alignment (contract 1.3.1).

A wide phone camera bends straight lines near the frame edges. No
homography can follow that, so with the whole projection in view the dots
near the edges miss by several projector px and the alignment fails as
`inaccurate` or `inconsistent`. The fix is to straighten camera points
before the homography:

    u = c + (p - c) * (1 + k * |p - c|^2 / r^2)

with `c` the frame center and `r` half its diagonal, so `k` is the relative
stretch at the corners. `k` is fitted only when the plain homography fails
(coded.py step 10b); a lens of `None` is the identity everywhere.

services/browser/lens.mjs mirrors this module operation for operation, so
both backends pick the same `k` bit for bit.
"""

from collections.abc import Callable, Mapping, Sequence
import math

from contracts.model import Point

LENS_K_LIMIT = 0.25          # |k|: keeps 1 + 3k > 0, so the map is monotonic
LENS_GRID = 50               # grid steps over [-limit, limit] (0.01 apart)
LENS_REFINE_STEPS = 40       # golden-section steps inside one grid cell
DISTORT_ITERATIONS = 12      # Newton steps of the inverse
INV_PHI = (math.sqrt(5.0) - 1.0) / 2.0


def lens_for(width: float, height: float, k: float) -> dict:
    """The lens of a `width` x `height` camera frame with stretch `k`."""

    cx = width / 2
    cy = height / 2
    return {"k": k, "cx": cx, "cy": cy, "r": math.sqrt(cx * cx + cy * cy)}


def valid_lens(lens) -> bool:
    """A lens dict with finite numbers, positive `r` and |k| within the limit."""

    if not isinstance(lens, Mapping):
        return False
    try:
        values = [float(lens[key]) for key in ("k", "cx", "cy", "r")]
    except (KeyError, TypeError, ValueError):
        return False
    return (all(math.isfinite(value) for value in values) and values[3] > 0
            and abs(values[0]) <= LENS_K_LIMIT)


def undistort(point: Point, lens: Mapping | None) -> Point:
    """Camera px -> straightened camera px. Identity for `None`."""

    if lens is None:
        return point
    cx, cy, r = lens["cx"], lens["cy"], lens["r"]
    dx = point.x - cx
    dy = point.y - cy
    scale = 1.0 + lens["k"] * ((dx * dx + dy * dy) / (r * r))
    return Point(cx + dx * scale, cy + dy * scale)


def distort(point: Point, lens: Mapping | None) -> Point:
    """Inverse of `undistort` (straightened px -> camera px).

    Radially, rho_u = rho * (1 + k rho^2) with rho = |p - c| / r; Newton's
    method solves it for rho from rho_u. The derivative 1 + 3 k rho^2 stays
    above 0.25 for |k| <= 0.25 out to rho = 1, the frame corners, so a fixed
    number of steps reaches float precision there (and both backends run
    the same steps).
    """

    if lens is None:
        return point
    cx, cy, r, k = lens["cx"], lens["cy"], lens["r"], lens["k"]
    ux = point.x - cx
    uy = point.y - cy
    straight = math.sqrt(ux * ux + uy * uy) / r
    if straight == 0.0:
        return Point(cx, cy)
    rho = straight
    for _ in range(DISTORT_ITERATIONS):
        slope = 1.0 + 3.0 * k * rho * rho
        if slope <= 0.0:
            break
        rho = rho - (rho * (1.0 + k * rho * rho) - straight) / slope
    ratio = rho / straight
    return Point(cx + ux * ratio, cy + uy * ratio)


def fit_lens(cost: Callable[[float], float]) -> float:
    """The `k` in [-limit, limit] with the lowest `cost(k)`.

    A grid 0.01 apart finds the right valley (the cost is not smooth: RANSAC
    picks inliers), then golden-section search refines inside the cells on
    either side of the best grid point. Ties keep the earlier `k`, so the
    result is deterministic and equal in both backends.
    """

    best_k, best_cost = 0.0, math.inf
    for step in range(LENS_GRID + 1):
        k = (step - LENS_GRID / 2) / (LENS_GRID / 2) * LENS_K_LIMIT
        value = cost(k)
        if value < best_cost:
            best_k, best_cost = k, value
    if not math.isfinite(best_cost):
        return 0.0
    cell = LENS_K_LIMIT / (LENS_GRID / 2)
    low = max(-LENS_K_LIMIT, best_k - cell)
    high = min(LENS_K_LIMIT, best_k + cell)
    a = high - INV_PHI * (high - low)
    b = low + INV_PHI * (high - low)
    cost_a, cost_b = cost(a), cost(b)
    for _ in range(LENS_REFINE_STEPS):
        for k, value in ((a, cost_a), (b, cost_b)):
            if value < best_cost:
                best_k, best_cost = k, value
        if cost_a <= cost_b:
            high, b, cost_b = b, a, cost_a
            a = high - INV_PHI * (high - low)
            cost_a = cost(a)
        else:
            low, a, cost_a = a, b, cost_b
            b = low + INV_PHI * (high - low)
            cost_b = cost(b)
    for k, value in ((a, cost_a), (b, cost_b)):
        if value < best_cost:
            best_k, best_cost = k, value
    return best_k


def undistort_all(points: Sequence[Point], lens: Mapping | None) -> list[Point]:
    return [undistort(point, lens) for point in points]


__all__ = ["DISTORT_ITERATIONS", "LENS_K_LIMIT", "distort", "fit_lens", "lens_for",
           "undistort", "undistort_all", "valid_lens"]
