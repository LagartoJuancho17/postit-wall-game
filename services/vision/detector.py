"""HSV segmentation of a stationary board while the projector is black.

The returned coordinates are camera pixels. A separate calibration service
maps them into projector pixels before the game uses them.

Every candidate quad is refined to sub-pixel accuracy on the saturation
image (``refine_quad``, contract ``docs/alignment-and-color.md`` section 2.4):
the color mask's boundary sits inside the blurred paper edge, so unrefined
outlines run 1-3 px small and their corners land several px off.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

from contracts import Board, Point, Polygon
from contracts.geometry import quadrilateral_center
from contracts.palette import ROLE_LABELS, ROLES, ColorRule, Palette

from .palette import DEFAULT_PALETTE

# How many notes of each role a playable board needs; bumpers are optional.
EXPECTED_NOTES: dict[str, int | None] = {"start": 1, "goal": 1, "bumper": None, "marker": 4}
MAX_NOTE_FRACTION = 0.05  # of the frame area


class DetectionError(ValueError):
    """The frame does not contain an unambiguous playable board."""

    def __init__(self, role: str, expected: int, found: int, *, shape: bool = False):
        self.role = role
        self.expected = expected
        self.found = found
        label = ROLE_LABELS[role]
        if shape:
            message = (f"Los post-its de {label} no forman un cuadrilátero. "
                       "Separalos hacia las cuatro esquinas del campo y volvé a escanear.")
        else:
            verb = "se esperaba" if expected == 1 else "se esperaban"
            message = (f"Post-its de {label}: {verb} {expected}; se detectaron {found}. "
                       f"Apagá la proyección, revisá la luz y el color elegido para {label}, "
                       "y volvé a escanear.")
        super().__init__(message)


@dataclass(frozen=True)
class _Candidate:
    polygon: Polygon   # refined when refine_quad accepted it, else the coarse quad
    center: Point
    area: float        # contour area: the filters and the largest-first order use it
    coarse: Polygon    # the quad before refinement (approxPolyDP or minAreaRect)
    outcome: str       # refine_quad's outcome: "refined" or why the coarse quad was kept


def _center(polygon: Polygon) -> Point:
    return quadrilateral_center(polygon)


# --------------------------------------------- sub-pixel outline refinement
#
# Contract section 2.4, steps 1-8. The JS port (services/browser/vision.mjs)
# mirrors this code operation by operation; the comments marked "parity" name
# every choice the contract leaves open that decides the last bit:
#
# - all arithmetic is float64; F is gathered from the uint8 channel and
#   converted, which is exactly "F as float64";
# - sums are plain left-to-right loops: numpy's sum is pairwise and Python
#   3.12's sum() is compensated, and neither matches a JS loop;
# - lengths use sqrt(dx*dx + dy*dy), never hypot (libms disagree in the last
#   bit); atan2, cos and sin may still differ by 1 ulp between libms, so a
#   parity test compares refined corners with a tolerance (1e-9 px), not bits;
# - medians are of 13 values (odd), so numpy's median is the 7th smallest.

REFINE_MIN_SIDE_PX = 8.0       # step 2: shorter sides keep the coarse quad
REFINE_LEVEL_REACH_PX = 3.0    # step 4: I from s <= -3, O from s >= 3; step 5 window
REFINE_MIN_CONTRAST = 20.0     # step 4: skip a sample whose I - O is below this
REFINE_MIN_POINTS = 3          # step 6: edge points a side needs
REFINE_MIN_CROSS = 0.2         # step 7: |sin| between adjacent fitted sides
REFINE_MAX_MOVE_PX = 4.0       # step 8: refined corner to coarse corner, inclusive, at least
REFINE_MAX_MOVE_SHARE = 0.15   # step 8: ... or this share of the coarse quad's shortest side
REFINE_AREA_RATIO = (0.85, 1.25)  # step 8: refined / coarse area, inclusive
# Step 4: s = -6.0, -5.75, ..., 6.0. Multiples of 0.25 are exact binary
# fractions, so these values and the comparisons with +-3.0 are exact.
REFINE_OFFSETS = np.array([-6.0 + 0.25 * index for index in range(49)], dtype=np.float64)
_INSIDE = slice(0, 13)       # s <= -3.0
_OUTSIDE = slice(36, 49)     # s >= 3.0
_FIRST_PAIR, _LAST_PAIR = 12, 35  # pairs (i, i + 1) with s_i >= -3.0 and s_(i+1) <= 3.0
REFINE_OUTCOMES = ("refined", "short_side", "few_points", "parallel", "moved", "concave", "area")


def _bilinear(feature: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Step 1: F at real coordinates, float64; pixel centers sit on integers.

    Parity: clamp ``x = min(max(x, 0), W - 1)`` (same for y), ``x0 =
    floor(x)``, ``x1 = min(x0 + 1, W - 1)``, ``fx = x - x0``; then ``top =
    F00 + fx * (F01 - F00)``, ``bottom = F10 + fx * (F11 - F10)``, ``value =
    top + fy * (bottom - top)``, where ``Fyx`` is ``F[y_y, x_x]``. No
    ``cv.remap``: its bilinear weights are 5-bit fixed point.
    """
    height, width = feature.shape
    xs = np.minimum(np.maximum(xs, 0.0), width - 1.0)
    ys = np.minimum(np.maximum(ys, 0.0), height - 1.0)
    x0, y0 = np.floor(xs), np.floor(ys)
    fx, fy = xs - x0, ys - y0
    ix0, iy0 = x0.astype(np.intp), y0.astype(np.intp)
    ix1, iy1 = np.minimum(ix0 + 1, width - 1), np.minimum(iy0 + 1, height - 1)
    f00 = feature[iy0, ix0].astype(np.float64)
    f01 = feature[iy0, ix1].astype(np.float64)
    f10 = feature[iy1, ix0].astype(np.float64)
    f11 = feature[iy1, ix1].astype(np.float64)
    top = f00 + fx * (f01 - f00)
    bottom = f10 + fx * (f11 - f10)
    return top + fy * (bottom - top)


def _sequential_sum(values) -> float:
    """Left-to-right float sum, the order a JS loop uses (see the parity notes)."""
    total = 0.0
    for value in values:
        total += value
    return total


def _twice_signed_area(points: list[tuple[float, float]]) -> float:
    """Shoelace sum over k = 0..3 of ``x_k * y_(k+1) - x_(k+1) * y_k``, in that order."""
    total = 0.0
    for index in range(4):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % 4]
        total += x1 * y2 - x2 * y1
    return total


def _edge_points(feature: np.ndarray, a: tuple[float, float], b: tuple[float, float],
                 centroid: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    """Steps 3-5 for the side from ``a`` to ``b``: its sub-pixel edge points (x, y)."""
    (ax, ay), (bx, by), (cx, cy) = a, b, centroid
    dx, dy = bx - ax, by - ay
    length = math.sqrt(dx * dx + dy * dy)
    # Step 3. Parity: n = (dy, -dx) / length, flipped when it points toward
    # the centroid as seen from a: n . (a - c) < 0. round() is half to even.
    nx, ny = dy / length, -dx / length
    if nx * (ax - cx) + ny * (ay - cy) < 0:
        nx, ny = -nx, -ny
    count = min(max(round(length / 4), 5), 25)
    # Parity: t = 0.15 + ((0.7 * j) / (K - 1)); p = a + t * (b - a).
    ts = np.array([0.15 + 0.7 * j / (count - 1) for j in range(count)])
    px, py = ax + ts * dx, ay + ts * dy
    # Step 4: profile F(p_j + s n), one row per sample, one column per s.
    profile = _bilinear(feature, px[:, None] + REFINE_OFFSETS[None, :] * nx,
                        py[:, None] + REFINE_OFFSETS[None, :] * ny)
    inside = np.median(profile[:, _INSIDE], axis=1)
    outside = np.median(profile[:, _OUTSIDE], axis=1)
    # Step 5: the first pair, in increasing s, with F(s_i) > m >= F(s_(i+1)).
    level = (inside + outside) / 2
    upper = profile[:, _FIRST_PAIR:_LAST_PAIR + 1]
    lower = profile[:, _FIRST_PAIR + 1:_LAST_PAIR + 2]
    crossing = (upper > level[:, None]) & (level[:, None] >= lower)
    rows = np.flatnonzero((inside - outside >= REFINE_MIN_CONTRAST) & crossing.any(axis=1))
    index = crossing[rows].argmax(axis=1) + _FIRST_PAIR
    f_i, f_next = profile[rows, index], profile[rows, index + 1]
    # Parity: s* = s_i + ((0.25 * (F_i - m)) / (F_i - F_(i+1))); e = p + s* n.
    offset = REFINE_OFFSETS[index] + 0.25 * (f_i - level[rows]) / (f_i - f_next)
    return px[rows] + offset * nx, py[rows] + offset * ny


def _fit_line(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float, float]:
    """Step 6: total least squares line (mean x, mean y, cos theta, sin theta)."""
    count = len(xs)
    xs, ys = [float(value) for value in xs], [float(value) for value in ys]
    mean_x = _sequential_sum(xs) / count
    mean_y = _sequential_sum(ys) / count
    sxx = _sequential_sum((x - mean_x) * (x - mean_x) for x in xs) / count
    syy = _sequential_sum((y - mean_y) * (y - mean_y) for y in ys) / count
    sxy = _sequential_sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / count
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return mean_x, mean_y, math.cos(theta), math.sin(theta)


def refine_quad(feature: np.ndarray, corners) -> tuple[np.ndarray, str]:
    """Contract 2.4 steps 2-9: move a coarse quad onto the sub-pixel edges of ``feature``.

    ``feature`` is F, a 2-D uint8 image (``find_notes`` passes the HSV
    saturation channel); ``corners`` is the coarse quad P, 4 (x, y) vertices
    in either orientation, pixel centers on integers. Returns ``(quad,
    outcome)``: the refined (4, 2) float64 corners in P's vertex order and
    ``"refined"``, or P itself (float64) and why it was kept:
    ``"short_side"`` (step 2), ``"few_points"`` (step 6), ``"parallel"``
    (step 7), or ``"moved"``, ``"concave"``, ``"area"`` (step 8, checked in
    that order). Step 9 runs steps 2-8 once more from the first result,
    still judged against P, and keeps the first result if that fails.
    """
    if not isinstance(feature, np.ndarray) or feature.ndim != 2 or feature.dtype != np.uint8:
        raise ValueError("feature must be a 2-D uint8 image")
    coarse = np.asarray(corners, dtype=np.float64).reshape(4, 2)
    if not np.all(np.isfinite(coarse)):
        raise ValueError("corners must be finite")
    points = [(float(x), float(y)) for x, y in coarse]
    first, outcome = _refine_pass(feature, points, points)
    if first is None:
        return coarse, outcome
    # Step 9. Where P is several px off at one end of a side, that end's
    # profiles miss the edge (I - O below the contrast floor) and the line
    # is fitted to half the side; sampling again along the first result
    # sees the whole side (measured 2026-10-20, a dark room: one corner
    # 6.3 px off after one pass, 1.6 px after two).
    second, _ = _refine_pass(feature, first, points)
    return np.array(second if second is not None else first, dtype=np.float64), "refined"


def _refine_pass(feature: np.ndarray, start: list[tuple[float, float]],
                 reference: list[tuple[float, float]]) -> tuple[list[tuple[float, float]] | None, str]:
    """Steps 2-8 sampling along ``start``, step 8 judged against ``reference`` (P)."""
    # Step 2. Parity: the centroid sums the vertices in order, then divides by 4.
    centroid = ((start[0][0] + start[1][0] + start[2][0] + start[3][0]) / 4,
                (start[0][1] + start[1][1] + start[2][1] + start[3][1]) / 4)
    for index in range(4):
        (ax, ay), (bx, by) = start[index], start[(index + 1) % 4]
        if math.sqrt((bx - ax) * (bx - ax) + (by - ay) * (by - ay)) < REFINE_MIN_SIDE_PX:
            return None, "short_side"
    lines = []
    for index in range(4):
        xs, ys = _edge_points(feature, start[index], start[(index + 1) % 4], centroid)
        if len(xs) < REFINE_MIN_POINTS:
            return None, "few_points"
        lines.append(_fit_line(xs, ys))
    # Step 7: corner k is where the lines of sides k - 1 and k meet. Parity:
    # q = M_(k-1) + u D_(k-1), u = ((M_k - M_(k-1)) x D_k) / (D_(k-1) x D_k).
    refined = []
    for index in range(4):
        m1x, m1y, d1x, d1y = lines[index - 1]
        m2x, m2y, d2x, d2y = lines[index]
        cross = d1x * d2y - d1y * d2x
        if abs(cross) < REFINE_MIN_CROSS:
            return None, "parallel"
        u = ((m2x - m1x) * d2y - (m2y - m1y) * d2x) / cross
        refined.append((m1x + u * d1x, m1y + u * d1y))
    # Step 8. A noisy coarse corner can sit several px off on a large note
    # while the fitted lines are right, so the reach scales with the note:
    # max(4 px, 15% of the shortest coarse side). Parity: squared distances
    # against reach * reach; side lengths with sqrt(dx*dx + dy*dy).
    shortest = math.inf
    for index in range(4):
        (ax, ay), (bx, by) = reference[index], reference[(index + 1) % 4]
        shortest = min(shortest, math.sqrt((bx - ax) * (bx - ax) + (by - ay) * (by - ay)))
    reach = max(REFINE_MAX_MOVE_PX, REFINE_MAX_MOVE_SHARE * shortest)
    for (qx, qy), (px, py) in zip(refined, reference):
        if (qx - px) * (qx - px) + (qy - py) * (qy - py) > reach * reach:
            return None, "moved"
    # Convex with P's orientation: every turn (q_k - q_(k-1)) x (q_(k+1) - q_k)
    # strictly has the sign of P's shoelace sum. A zero turn is not convex,
    # and a P with a zero shoelace sum has no orientation: both keep P.
    coarse_area = _twice_signed_area(reference)
    for index in range(4):
        (x0, y0), (x1, y1), (x2, y2) = refined[index - 1], refined[index], refined[(index + 1) % 4]
        turn = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1)
        if not ((coarse_area > 0 and turn > 0) or (coarse_area < 0 and turn < 0)):
            return None, "concave"
    # Parity: the ratio of the two shoelace sums (the halves cancel exactly).
    ratio = _twice_signed_area(refined) / coarse_area
    if not REFINE_AREA_RATIO[0] <= ratio <= REFINE_AREA_RATIO[1]:
        return None, "area"
    return refined, "refined"


def _polygon_from_contour(contour: np.ndarray) -> Polygon:
    perimeter = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        vertices = approx.reshape(4, 2).astype(np.float32)
    else:
        vertices = cv2.boxPoints(cv2.minAreaRect(contour))
    return Polygon(tuple(Point(float(x), float(y)) for x, y in vertices))


def _mask(hsv: np.ndarray, rule: ColorRule) -> np.ndarray:
    floor_s, floor_v = rule.min_saturation, rule.min_value
    if rule.hue_low <= rule.hue_high:
        return cv2.inRange(hsv, (rule.hue_low, floor_s, floor_v), (rule.hue_high, 255, 255))
    return (cv2.inRange(hsv, (rule.hue_low, floor_s, floor_v), (179, 255, 255))
            | cv2.inRange(hsv, (0, floor_s, floor_v), (rule.hue_high, 255, 255)))


def _refined(feature: np.ndarray, coarse: Polygon) -> tuple[Polygon, str]:
    quad, outcome = refine_quad(feature, [(point.x, point.y) for point in coarse.points])
    if outcome != "refined":
        return coarse, outcome
    return Polygon(tuple(Point(float(x), float(y)) for x, y in quad)), outcome


BRIDGE_KERNEL = np.ones((5, 5), dtype=np.uint8)
CLEAN_KERNEL = np.ones((3, 3), dtype=np.uint8)


def _clean(mask: np.ndarray) -> np.ndarray:
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, CLEAN_KERNEL)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, CLEAN_KERNEL)


def _is_note_shape(contour: np.ndarray, min_area: float, max_area: float) -> bool:
    """The size and shape filters every candidate passes (``_find_candidates``)."""
    area = cv2.contourArea(contour)
    if area < min_area or area > max_area:
        return False
    (_, _), (rect_width, rect_height), _ = cv2.minAreaRect(contour)
    short_side = min(rect_width, rect_height)
    long_side = max(rect_width, rect_height)
    if short_side < 5 or long_side / short_side > 4.0:
        return False
    return area / (rect_width * rect_height) >= 0.55


def _joined(contours, raw: np.ndarray, bridge: np.ndarray, min_area: float,
            max_area: float) -> list[np.ndarray]:
    """Contract 2.5: the pieces of a paper our own rim cut become one, their convex hull.

    White light saturates a paper a rim crosses and leaves a colorless gap
    that splits it. In ``joined`` the rim pixels next to the color (within
    2 px) count as the color, so the pieces share a component. A component
    with two or more pieces that are notes by themselves (they pass every
    candidate filter) stays apart: that is two papers with our rim between
    them, and joining them made the count flip every frame (review,
    2026-10-21). A piece that is not a note (a strip a rim cut off, in a dark
    room the part our light does not reach) joins its note. A contour alone
    in its group is returned as is, so a rim beside a paper never moves its
    outline. Opening and closing are increasing, so every contour pixel lies
    in ``joined`` (label >= 1).
    """
    joined = _clean(raw | (bridge & cv2.dilate(raw, BRIDGE_KERNEL)))
    _, labels = cv2.connectedComponents(joined, connectivity=8)
    groups: dict[int, list[np.ndarray]] = {}
    for contour in contours:
        x, y = contour[0, 0]
        groups.setdefault(int(labels[y, x]), []).append(contour)
    out: list[np.ndarray] = []
    for members in groups.values():
        notes = sum(1 for member in members if _is_note_shape(member, min_area, max_area))
        if len(members) == 1 or notes >= 2:
            out.extend(members)
        else:
            out.append(cv2.convexHull(np.vstack(members)))
    return out


def _find_candidates(hsv: np.ndarray, rule: ColorRule,
                     bridge: np.ndarray | None = None) -> list[_Candidate]:
    height, width = hsv.shape[:2]
    raw = _mask(hsv, rule)
    mask = _clean(raw)

    # At 1080p the floor is roughly 250 px². This rejects isolated hot
    # pixels without depending on a particular webcam resolution.
    min_area = max(80.0, width * height * 0.00012)
    # A note is a small part of a wall that also shows the whole field. A
    # bigger blob is a tinted wall or a large object, never a post-it.
    max_area = width * height * MAX_NOTE_FRACTION
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if bridge is not None:
        contours = _joined(contours, raw, bridge, min_area, max_area)
    candidates: list[_Candidate] = []
    for contour in contours:
        if not _is_note_shape(contour, min_area, max_area):
            continue
        area = cv2.contourArea(contour)
        coarse = _polygon_from_contour(contour)
        # Contract 2.4: F is the saturation channel of this same HSV image.
        polygon, outcome = _refined(hsv[..., 1], coarse)
        candidates.append(_Candidate(polygon, _center(polygon), area, coarse, outcome))
    return sorted(candidates, key=lambda candidate: candidate.area, reverse=True)


def _ordered_markers(markers: list[_Candidate]) -> tuple[_Candidate, ...]:
    """Order four marker centers clockwise, beginning at camera top-left."""
    centers = np.array([(m.center.x, m.center.y) for m in markers], dtype=np.float32)
    hull = cv2.convexHull(centers).reshape(-1, 2)
    if len(hull) != 4 or cv2.contourArea(hull) < 100:
        raise DetectionError("marker", 4, len(hull), shape=True)
    centroid = centers.mean(axis=0)
    indices = sorted(
        range(4),
        key=lambda index: np.arctan2(
            centers[index, 1] - centroid[1], centers[index, 0] - centroid[0]
        ),
    )
    start = min(range(4), key=lambda offset: centers[indices[offset]].sum())
    indices = indices[start:] + indices[:start]
    return tuple(markers[index] for index in indices)


def _candidates_by_role(frame_bgr: np.ndarray, palette: Palette,
                        bridge: np.ndarray | None = None) -> dict[str, list[_Candidate]]:
    if not isinstance(frame_bgr, np.ndarray) or frame_bgr.ndim != 3:
        raise ValueError("frame_bgr debe ser una imagen BGR de tres canales")
    if frame_bgr.shape[2] != 3 or frame_bgr.dtype != np.uint8:
        raise ValueError("frame_bgr debe tener dtype uint8 y tres canales BGR")
    if min(frame_bgr.shape[:2]) < 20:
        raise ValueError("La imagen de la webcam es demasiado pequeña")
    if not isinstance(palette, Palette):
        raise TypeError("palette debe ser un contracts.palette.Palette")
    if bridge is not None and (bridge.shape != frame_bgr.shape[:2] or bridge.dtype != np.uint8):
        raise ValueError("bridge debe ser una máscara uint8 del tamaño del cuadro")
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    return {role: _find_candidates(hsv, palette.rule(role), bridge) for role in ROLES}


def find_notes(frame_bgr: np.ndarray, palette: Palette = DEFAULT_PALETTE,
               bridge: np.ndarray | None = None) -> dict[str, tuple[Polygon, ...]]:
    """Every note of each role that passes the shape filters, largest first.

    Unlike ``detect_board`` it never requires a count, so a live preview can
    show a partial or ambiguous wall. The filters and colors are identical.
    ``bridge`` (uint8, 255 where the projector draws its own rims) joins
    the pieces of a paper one of those rims cuts (contract 2.5); it never
    moves an outline.
    """
    found = _candidates_by_role(frame_bgr, palette, bridge)
    return {role: tuple(candidate.polygon for candidate in found[role]) for role in ROLES}


def detect_board(frame_bgr: np.ndarray, palette: Palette = DEFAULT_PALETTE) -> Board:
    """Return visible notes and marker centers from a projector-dark frame.

    Four markers, one start and one goal are required; bumpers are optional.
    ``palette`` says which colors count for each role; the default is fuchsia
    or violet markers, a yellow start, an orange goal and green bumpers.
    Raise ``DetectionError`` for a missing or ambiguous required role.
    ``playfield`` consists of marker centers in camera order: top-left,
    top-right, bottom-right, bottom-left.
    """
    found = _candidates_by_role(frame_bgr, palette)
    for role in ("marker", "start", "goal"):
        if len(found[role]) != EXPECTED_NOTES[role]:
            raise DetectionError(role, EXPECTED_NOTES[role], len(found[role]))

    ordered = _ordered_markers(found["marker"])
    return Board(
        playfield=Polygon(tuple(marker.center for marker in ordered)),
        start=found["start"][0].polygon,
        goal=found["goal"][0].polygon,
        bumpers=tuple(candidate.polygon for candidate in found["bumper"]),
        markers=tuple(marker.polygon for marker in ordered),
    )
