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
grid; `services/calibration/evals/touch.py` measures both.

Hands are not CCV's fingers on a touch table: a post-it lands a few mm off
its cross, and now and then one lands well off. The first real run
(2026-10-27, 1920 x 1080, a 4 x 4 grid) scattered ~4 px per axis with three
touches 16-24 px off. So a robust fit (least median of squares) names the
touches that disagree and the run asks for just those again, instead of
starting over; the gate is the error the outline will have (the model's),
not how far one hand missed its cross.

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
# A touch disagrees with the rest when its residual in the robust fit is
# over TOUCH_OUTLIER_SIGMA times the run's own noise per axis (a careful hand
# and a hurried one each get their own bar), and over TOUCH_OUTLIER_MIN_PX.
TOUCH_OUTLIER_SIGMA = 4.5
TOUCH_OUTLIER_MIN_PX = 8.0
# Expected error of the fitted mapping itself (model_rms_px below): what the
# outline is off by, as opposed to how far one hand missed its cross.
TOUCH_MAX_MODEL_PX = 3.5
TOUCH_MIN_POINTS = 9
TOUCH_MAX_SUBSETS = 4096         # 4-touch subsets tried by the robust start
TOUCH_REFITS = 2                 # least-squares passes after it
TOUCH_RETAKE_LOOSE = 2           # asked again when none is off but the model is loose
TOUCH_MAX_SHIFT_PX = 2.0         # the touches off move the outline less than this: not worth a retake
TOUCH_MAX_ROUNDS = 3             # rounds of retakes before the run gives up

TOUCH_REASONS = {
    "touch_retake": (
        "Algunos toques no coinciden con los demás: repetí {points}. Pegá el post-it centrado en la "
        "cruz y sacá la mano."
    ),
    "touch_inaccurate": (
        "Los toques están muy dispersos: el contorno quedaría a unos {error:.1f} px de cada papel. "
        "Pegá cada post-it centrado en su cruz y no muevas la cámara ni el proyector."
    ),
    "touch_invalid": (
        "Los toques no forman la grilla de la proyección. Poné el post-it sobre la cruz que "
        "marca el círculo, en orden."
    ),
}


def points_text(numbers: list[int]) -> str:
    """'el punto 3' / 'los puntos 3, 7 y 11' (1-based numbers)."""
    if len(numbers) == 1:
        return f"el punto {numbers[0]}"
    return "los puntos " + ", ".join(str(n) for n in numbers[:-1]) + f" y {numbers[-1]}"


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
            "captured": [], "retake": [], "rounds": 0, "baseline": list(baseline), "phase": "waiting", "rebase": False,
            "sum_x": 0.0, "sum_y": 0.0, "samples": 0, "since": None, "many": False}


def touch_step(run: dict) -> int:
    """The cross armed now: the next touch to retake, else the next new one."""
    return run["retake"][0] if run["retake"] else len(run["captured"])


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
        point = Point(run["sum_x"] / run["samples"], run["sum_y"] / run["samples"])
        captured = list(run["captured"])
        retake = run["retake"]
        if retake:
            captured[retake[0]] = point
            retake = retake[1:]
        else:
            captured.append(point)
        run = {**_idle(run, "lift"), "captured": captured, "retake": retake}
        return run, "done" if not retake and len(captured) == len(run["targets"]) else "captured"
    return run, None


def touch_back(run: dict) -> dict:
    """CCV's [r]: the previous cross is armed again. Retakes go in the order
    asked, so there it only restarts the hold."""
    if run["retake"]:
        return _idle(run, "waiting")
    return {**_idle(run, "waiting"), "captured": run["captured"][:-1]}


def touch_retake(run: dict, indices: list[int]) -> dict:
    """The run goes on with these touches (0-based) to take again. The phase
    stays as it is: the last post-it is still on the wall and has to leave
    before the first retake arms."""
    return {**run, "retake": list(indices), "rounds": run["rounds"] + 1}


def touch_progress(run: dict, now: float) -> float:
    if run["phase"] != "holding" or run["since"] is None:
        return 0.0
    return min(1.0, max(0.0, (now - run["since"]) / TOUCH_HOLD_S))


def fit_homography(camera: list[Point], projector: list[Point]):
    """Least squares over the given touches."""
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


def _homography4(camera: list[Point], projector: list[Point]):
    """The homography through 4 touches exactly: the 8 x 8 DLT system solved
    by Gaussian elimination with partial pivoting, in plain floats so the
    browser port gets the same bits. None when degenerate."""
    rows = []
    for c, q in zip(camera, projector):
        rows.append([c.x, c.y, 1.0, 0.0, 0.0, 0.0, -q.x * c.x, -q.x * c.y, q.x])
        rows.append([0.0, 0.0, 0.0, c.x, c.y, 1.0, -q.y * c.x, -q.y * c.y, q.y])
    for col in range(8):
        pivot = col
        for r in range(col + 1, 8):
            if abs(rows[r][col]) > abs(rows[pivot][col]):
                pivot = r
        if abs(rows[pivot][col]) <= 1e-9:
            return None
        rows[col], rows[pivot] = rows[pivot], rows[col]
        for r in range(col + 1, 8):
            factor = rows[r][col] / rows[col][col]
            if factor != 0.0:
                for k in range(col, 9):
                    rows[r][k] -= factor * rows[col][k]
    h = [0.0] * 8
    for col in range(7, -1, -1):
        total = rows[col][8]
        for k in range(col + 1, 8):
            total -= rows[col][k] * h[k]
        h[col] = total / rows[col][col]
    if not all(math.isfinite(v) for v in h):
        return None
    return h


def _residual(h: list[float], camera: Point, projector: Point) -> float:
    w = h[6] * camera.x + h[7] * camera.y + 1.0
    if abs(w) <= 1e-12:
        return math.inf
    dx = (h[0] * camera.x + h[1] * camera.y + h[2]) / w - projector.x
    dy = (h[3] * camera.x + h[4] * camera.y + h[5]) / w - projector.y
    return math.sqrt(dx * dx + dy * dy)


def _subsets(n: int, targets: list[Point]) -> list[tuple[int, int, int, int]]:
    """Every 4-touch subset; on a big grid, one touch from each quarter of it
    (the well-spread ones), evenly thinned to TOUCH_MAX_SUBSETS."""
    if n * (n - 1) * (n - 2) * (n - 3) // 24 <= TOUCH_MAX_SUBSETS:
        return [(a, b, c, d) for a in range(n) for b in range(a + 1, n)
                for c in range(b + 1, n) for d in range(c + 1, n)]
    xs = [t.x for t in targets]
    ys = [t.y for t in targets]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    quarters = [[i for i, t in enumerate(targets) if t.x != cx and t.y != cy and (t.x > cx, t.y > cy) == key]
                for key in ((False, False), (True, False), (False, True), (True, True))]
    combos = [(a, b, c, d) for a in quarters[0] for b in quarters[1] for c in quarters[2] for d in quarters[3]]
    stride = -(-len(combos) // TOUCH_MAX_SUBSETS)
    return combos[::stride]


def _robust_start(camera: list[Point], targets: list[Point]):
    """Least median of squares (Rousseeuw 1984): of the exact homographies
    through 4 touches, the one whose residuals have the smallest median.
    Up to half of the touches can be off without moving it, where least
    squares is dragged by every slip and then blames its neighbors. The
    first best subset wins a tie. Returns (h, residuals) or None."""
    n = len(camera)
    rank = (n + 5) // 2 - 1
    best = None
    for subset in _subsets(n, targets):
        h = _homography4([camera[i] for i in subset], [targets[i] for i in subset])
        if h is None:
            continue
        residuals = [_residual(h, c, t) for c, t in zip(camera, targets)]
        if not all(math.isfinite(r) for r in residuals):
            continue
        criterion = sorted(r * r for r in residuals)[rank]
        if best is None or criterion < best[0]:
            best = (criterion, h, residuals)
    return None if best is None else (best[1], best[2], best[0])


def _sum_squares(values: list[float]) -> float:
    total = 0.0
    for v in values:
        total += v * v
    return total


def solve_touch(camera: list[Point], targets: list[Point], width: int, height: int) -> dict:
    """The homography from the touches, or why not (contract 1.6).

    Least squares over every touch is the answer: dropping a touch, a corner
    above all, leaves that part of the projection to extrapolation and costs
    more than the touch's own error (evals/touch.py measured both). So a
    robust fit only points at the touches that disagree: least median of
    squares over 4-touch subsets, then least squares over the touches it
    keeps, whose residuals give the run's noise per axis. A touch farther
    than TOUCH_OUTLIER_SIGMA of that (and TOUCH_OUTLIER_MIN_PX) from the
    robust fit comes back in `retake` (0-based, code touch_retake, up to half
    of them) and the run asks for it again, unless they bend the outline by
    TOUCH_MAX_SHIFT_PX or less. The run passes when the model's own error is
    within TOUCH_MAX_MODEL_PX and nothing worth retaking is off; otherwise
    asks again for the TOUCH_RETAKE_LOOSE touches farthest from the robust
    fit (with none off). More than half off is touch_inaccurate; no grid at
    all, or in another order, are touch_invalid."""
    total = len(targets)
    result = {"ok": False, "code": None, "reason": None, "matrix": None, "decoded": len(camera), "total": total,
              "fit": None, "inliers": None, "holdout": None, "holdout_p95_px": None, "holdout_max_px": None,
              "outliers": None, "coverage": None, "retake": None, "fit_rms_px": None, "model_rms_px": None,
              "outlier_bar_px": None, "shift_px": None}

    def fail(code: str, **values) -> dict:
        result.update(ok=False, code=code, reason=TOUCH_REASONS[code].format(**values), matrix=None)
        return result

    n = len(camera)
    if n < TOUCH_MIN_POINTS:
        return fail("touch_invalid")
    start = _robust_start(camera, targets)
    if start is None:
        return fail("touch_invalid")
    _, errors, criterion = start
    without = None
    # The median 2-D residual of a Gaussian hand is ~1.18 sigma per axis.
    sigma = math.sqrt(criterion) / 1.18
    for _ in range(TOUCH_REFITS):
        bar = max(TOUCH_OUTLIER_SIGMA * sigma, TOUCH_OUTLIER_MIN_PX)
        kept = [i for i in range(n) if errors[i] <= bar]
        if len(kept) < TOUCH_MIN_POINTS:
            kept = sorted(range(n), key=lambda i: (errors[i], i))[:TOUCH_MIN_POINTS]
        robust = fit_homography([camera[i] for i in kept], [targets[i] for i in kept])
        if robust is None:
            return fail("touch_invalid")
        without = robust
        errors = [_error(robust, c, t) for c, t in zip(camera, targets)]
        # Never above the robust start's: slips that got in would raise the
        # bar that is meant to keep them out.
        sigma = min(sigma, math.sqrt(_sum_squares([errors[i] for i in kept]) / (2 * len(kept) - 8)))
    bar = max(TOUCH_OUTLIER_SIGMA * sigma, TOUCH_OUTLIER_MIN_PX)
    off = [i for i in range(n) if errors[i] > bar]
    result["outlier_bar_px"] = bar
    matrix = fit_homography(camera, targets)
    if matrix is None or _jacobian_sign(matrix, camera) <= 0:
        return fail("touch_invalid")   # mirrored: touches in another order
    fit_errors = [_error(matrix, c, q) for c, q in zip(camera, targets)]
    total_sq = _sum_squares(fit_errors)
    held = leave_one_out(camera, targets)
    finite = sorted(e for e in held if math.isfinite(e))
    result.update(fit=n, holdout=n, inliers=n - len(off), outliers=len(off),
                  holdout_p95_px=(_sorted_percentile(np.array(finite, dtype=np.float64), 95)
                                  if len(finite) == len(held) else math.inf),
                  holdout_max_px=max(held), fit_rms_px=math.sqrt(total_sq / n),
                  # Least squares with 8 unknowns over 2n coordinates: the
                  # residuals give the noise per coordinate, and the fitted
                  # mapping carries 8/(2n) of its variance on average at the
                  # touches (the leverages sum to 8).
                  model_rms_px=math.sqrt(8.0 * total_sq / ((2 * n - 8) * n)))
    xs = [q.x for q in targets]
    ys = [q.y for q in targets]
    result["coverage"] = (max(xs) - min(xs)) * (max(ys) - min(ys)) / (width * height)
    # How far the touches that are off bend the outline: the fit with every
    # touch against the robust one without them, at every touch.
    shift = 0.0
    for c in camera:
        try:
            a, b = transform_point(c, matrix), transform_point(c, without)
        except ValueError:
            shift = math.inf
            break
        shift = max(shift, _distance(a, b))
    result["shift_px"] = shift
    if result["model_rms_px"] <= TOUCH_MAX_MODEL_PX and (not off or shift <= TOUCH_MAX_SHIFT_PX):
        result.update(ok=True, matrix=matrix)
        return result
    if not off:
        # Every touch is within the bar but the mapping is loose: the touches
        # farthest from the robust fit are the likeliest small slips.
        off = sorted(sorted(range(n), key=lambda i: (-errors[i], i))[:TOUCH_RETAKE_LOOSE])
    if len(off) <= n // 2:
        result["retake"] = off
        return fail("touch_retake", points=points_text([i + 1 for i in off]))
    return fail("touch_inaccurate", error=result["model_rms_px"])


def touch_give_up(result: dict) -> dict:
    """A touch_retake after TOUCH_MAX_ROUNDS rounds: the run ends instead."""
    return {**result, "code": "touch_inaccurate", "retake": None,
            "reason": TOUCH_REASONS["touch_inaccurate"].format(error=result["model_rms_px"])}
