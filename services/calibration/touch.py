"""Touch-grid alignment (contract 1.6), after NUI Group's Community Core Vision.

CCV (tbeta 1.x, `ofxNCore/src/Calibration`) projects a grid of crosses; the
operator touches each one and holds until a circle fills, and the touch
blob's centroid becomes that point's camera position. Here the "touch" is a
post-it held centered on the projected cross: the camera already finds
post-its by color, in a lit room too, where the white coded dots (1.3) lose
contrast.

What is kept from CCV: the row-major grid of targets inset from the edges,
touch and hold with a filling circle, `B` to retake the background (the notes
already on the wall), `R` to go back one point.

What is not: CCV maps a camera point to the screen by barycentric
interpolation inside the touched triangle mesh. Between touches that is
linear, and a camera far off the projector's axis (Tobias: 2 m below it)
sees each grid cell as a perspective quad, not an affine one. On a flat wall
one homography fitted to every touch is exact everywhere, also outside the
grid; `services/calibration/evals/touch.py` measures both. The touches go
through the same leave-one-out check the dot method uses on its holdout.

Pure functions: the server and the browser port (services/browser/touch.mjs)
keep the run as a dict and call these on every live frame. Sums are left to
right and percentiles use coded.py's numpy-exact helper, so both get the
same bits.
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from contracts.model import Point
from services.calibration import transform_point
from services.calibration.coded import _sorted_percentile

TOUCH_GRID = (4, 4)              # CCV's default calibration.xml: 3 x 3 cells, 4 x 4 points
TOUCH_GRID_MIN = (3, 3)
TOUCH_GRID_MAX = (8, 6)
TOUCH_MARGIN = 0.12              # share of the projection kept free around the grid: a post-it fits outside it
TOUCH_HOLD_S = 1.0               # still this long under one cross to capture it
TOUCH_MIN_SAMPLES = 3            # frames averaged into one capture
TOUCH_STILL_SHARE = 0.012        # of the camera's short side: farther from the running mean restarts the hold
TOUCH_NEW_SHARE = 0.03           # of the camera's short side: this close to a background note is that note
# Leave-one-out error, projector px. A hand centers a post-it on a cross
# within ~2 mm (~1 px at 2.4 m / 1280 px); leaving a corner out makes the
# others extrapolate it, so careful runs reach a p95 of ~4.5 and a max of ~6
# and a 25 mm slip ~13 (evals/touch.py measured these).
TOUCH_MAX_P95_PX = 6.0
TOUCH_MAX_ERROR_PX = 9.0
TOUCH_OUTLIER_PX = 9.0           # one touch farther than this is dropped if the rest then pass
TOUCH_MIN_POINTS = 9

TOUCH_REASONS = {
    "touch_inaccurate": (
        "Los toques no coinciden entre sí: el punto {worst} quedó corrido unos {error:.1f} px. "
        "Centrá el post-it en cada cruz y no muevas la cámara ni el proyector."
    ),
    "touch_invalid": (
        "Los toques no forman la grilla de la proyección. Poné el post-it sobre la cruz que "
        "marca el círculo, en orden."
    ),
}


def touch_targets(width: int, height: int, cols: int, rows: int) -> list[Point]:
    """Row-major grid of crosses in projector px, TOUCH_MARGIN in from each edge."""
    span = 1.0 - 2.0 * TOUCH_MARGIN
    return [Point((TOUCH_MARGIN + span * i / (cols - 1)) * width, (TOUCH_MARGIN + span * j / (rows - 1)) * height)
            for j in range(rows) for i in range(cols)]


def valid_grid(cols: object, rows: object) -> bool:
    return (isinstance(cols, int) and isinstance(rows, int) and not isinstance(cols, bool)
            and not isinstance(rows, bool) and TOUCH_GRID_MIN[0] <= cols <= TOUCH_GRID_MAX[0]
            and TOUCH_GRID_MIN[1] <= rows <= TOUCH_GRID_MAX[1])


def note_centers(notes, roles) -> list[Point]:
    """Every note's vertex mean, role by role in `roles` order (notes: role ->
    polygons, as the detector returns them)."""
    centers = []
    for role in roles:
        for polygon in notes.get(role, ()):
            points = polygon.points
            sx = sy = 0.0
            for point in points:
                sx += point.x
                sy += point.y
            centers.append(Point(sx / len(points), sy / len(points)))
    return centers


def _distance(a: Point, b: Point) -> float:
    dx, dy = a.x - b.x, a.y - b.y
    return math.sqrt(dx * dx + dy * dy)


def new_notes(centers: list[Point], baseline: list[Point], camera_size: tuple[int, int]) -> list[Point]:
    """Centers not at a background note: the post-it being held."""
    near = TOUCH_NEW_SHARE * min(camera_size)
    return [c for c in centers if all(_distance(c, b) > near for b in baseline)]


def touch_begin(width: int, height: int, cols: int, rows: int, baseline: list[Point]) -> dict:
    return {"cols": cols, "rows": rows, "targets": touch_targets(width, height, cols, rows),
            "captured": [], "baseline": list(baseline), "phase": "waiting", "rebase": False,
            "sum_x": 0.0, "sum_y": 0.0, "samples": 0, "since": None, "many": False}


def _idle(run: dict, phase: str) -> dict:
    return {**run, "phase": phase, "sum_x": 0.0, "sum_y": 0.0, "samples": 0, "since": None}


def touch_update(run: dict, centers: list[Point], camera_size: tuple[int, int], now: float) -> tuple[dict, str | None]:
    """One live frame. Returns the new run and its event: "background",
    "captured", "done" (the last point captured) or None."""
    if run["rebase"]:
        return {**_idle(run, "waiting"), "baseline": list(centers), "rebase": False, "many": False}, "background"
    fresh = new_notes(centers, run["baseline"], camera_size)
    run = {**run, "many": len(fresh) > 1}
    if run["phase"] == "lift":
        # The captured post-it must leave before the next cross arms: it
        # would otherwise be captured again where it is.
        return (_idle(run, "waiting") if not fresh else run), None
    if len(fresh) != 1:
        return _idle(run, "waiting"), None
    point = fresh[0]
    if run["samples"]:
        mean = Point(run["sum_x"] / run["samples"], run["sum_y"] / run["samples"])
        if _distance(point, mean) > TOUCH_STILL_SHARE * min(camera_size):
            run = _idle(run, "waiting")   # moved: the hold starts over here
    if not run["samples"]:
        run = {**run, "since": now}
    run = {**run, "phase": "holding", "sum_x": run["sum_x"] + point.x, "sum_y": run["sum_y"] + point.y,
           "samples": run["samples"] + 1}
    if now - run["since"] >= TOUCH_HOLD_S and run["samples"] >= TOUCH_MIN_SAMPLES:
        captured = [*run["captured"], Point(run["sum_x"] / run["samples"], run["sum_y"] / run["samples"])]
        run = {**_idle(run, "lift"), "captured": captured}
        return run, "done" if len(captured) == len(run["targets"]) else "captured"
    return run, None


def touch_back(run: dict) -> dict:
    """CCV's [r]: the previous cross is armed again."""
    return {**_idle(run, "waiting"), "captured": run["captured"][:-1]}


def touch_progress(run: dict, now: float) -> float:
    if run["phase"] != "holding" or run["since"] is None:
        return 0.0
    return min(1.0, max(0.0, (now - run["since"]) / TOUCH_HOLD_S))


def fit_homography(camera: list[Point], projector: list[Point]):
    """Least squares over every touch (no RANSAC: every touch was confirmed)."""
    source = np.array([[p.x, p.y] for p in camera], dtype=np.float64)
    target = np.array([[p.x, p.y] for p in projector], dtype=np.float64)
    try:
        matrix, _ = cv2.findHomography(source, target, 0)
    except cv2.error:
        return None
    if matrix is None or matrix.shape != (3, 3) or not np.isfinite(matrix).all() or abs(matrix[2, 2]) <= 1e-12:
        return None
    return [[float(matrix[r, c] / matrix[2, 2]) for c in range(3)] for r in range(3)]


def _error(matrix, camera: Point, projector: Point) -> float:
    try:
        mapped = transform_point(camera, matrix)
    except ValueError:
        return math.inf
    return _distance(mapped, projector)


def _jacobian_sign(matrix, camera: list[Point]) -> float:
    """Sign of the mapping's Jacobian at the touches' mean: the camera does not
    mirror the wall, so a negative sign means touches in another order (a
    transposed square grid fits a homography too)."""
    sx = sy = 0.0
    for point in camera:
        sx += point.x
        sy += point.y
    x, y = sx / len(camera), sy / len(camera)
    (a, b, c), (d, e, f), (g, h, i) = matrix
    w = g * x + h * y + i
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return det / (w * w * w)


def leave_one_out(camera: list[Point], projector: list[Point]) -> list[float]:
    """Error at each touch of the homography fitted to all the others."""
    errors = []
    for k in range(len(camera)):
        matrix = fit_homography(camera[:k] + camera[k + 1:], projector[:k] + projector[k + 1:])
        errors.append(math.inf if matrix is None else _error(matrix, camera[k], projector[k]))
    return errors


def _evaluate(camera: list[Point], projector: list[Point]) -> dict:
    errors = leave_one_out(camera, projector)
    finite = sorted(e for e in errors if math.isfinite(e))
    p95 = _sorted_percentile(np.array(finite, dtype=np.float64), 95) if len(finite) == len(errors) else math.inf
    worst = max(range(len(errors)), key=lambda k: (errors[k], -k))
    return {"errors": errors, "p95": p95, "max": errors[worst], "worst": worst}


def solve_touch(camera: list[Point], targets: list[Point], width: int, height: int) -> dict:
    """The homography from the touches, or why not (contract 1.6)."""
    total = len(targets)
    result = {"ok": False, "code": None, "reason": None, "matrix": None, "decoded": len(camera), "total": total,
              "fit": None, "inliers": None, "holdout": None, "holdout_p95_px": None, "holdout_max_px": None,
              "outliers": None, "coverage": None, "dropped": None, "fit_rms_px": None}

    def fail(code: str, **values) -> dict:
        result.update(ok=False, code=code, reason=TOUCH_REASONS[code].format(**values), matrix=None)
        return result

    kept = list(range(len(camera)))
    measured = _evaluate(camera, targets) if len(camera) >= TOUCH_MIN_POINTS else None
    if measured is None or not math.isfinite(measured["p95"]):
        return fail("touch_invalid")
    if (measured["max"] > TOUCH_OUTLIER_PX and len(camera) - 1 >= TOUCH_MIN_POINTS):
        # One misplaced touch is dropped when the others agree without it.
        k = measured["worst"]
        rest = [i for i in kept if i != k]
        again = _evaluate([camera[i] for i in rest], [targets[i] for i in rest])
        if again["p95"] <= TOUCH_MAX_P95_PX and again["max"] <= TOUCH_MAX_ERROR_PX:
            kept, measured, result["dropped"] = rest, again, k + 1
    cam, proj = [camera[i] for i in kept], [targets[i] for i in kept]
    matrix = fit_homography(cam, proj)
    result.update(fit=len(cam), holdout=len(cam), holdout_p95_px=measured["p95"], holdout_max_px=measured["max"])
    if matrix is None:
        return fail("touch_invalid")
    if _jacobian_sign(matrix, cam) <= 0:
        return fail("touch_invalid")   # a mirrored grid: touches in another order
    fit_errors = [_error(matrix, c, p) for c, p in zip(cam, proj)]
    total_sq = 0.0
    for e in fit_errors:
        total_sq += e * e
    result["fit_rms_px"] = math.sqrt(total_sq / len(fit_errors))
    result["inliers"] = sum(1 for e in fit_errors if e <= TOUCH_OUTLIER_PX)
    result["outliers"] = len(camera) - len(cam)
    xs = [p.x for p in proj]
    ys = [p.y for p in proj]
    result["coverage"] = (max(xs) - min(xs)) * (max(ys) - min(ys)) / (width * height)
    if measured["p95"] > TOUCH_MAX_P95_PX or measured["max"] > TOUCH_MAX_ERROR_PX:
        return fail("touch_inaccurate", worst=kept[measured["worst"]] + 1, error=measured["max"])
    result.update(ok=True, matrix=matrix)
    return result
