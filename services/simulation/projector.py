"""What the projector window draws, re-implemented in Python, in projector px.

Rule sets, chosen with ``rules``:

- ``"old"``: the projector before the alignment contract (commit 8238278):
  ``drawLiveDetection`` (still the panel's overlay in
  ``services/web/ui/live-view.mjs``) plus the old ``drawLiveGame``. Every
  note is mapped with the contain transform and filled with its role color
  at ``globalAlpha .3`` with an 18 px colored ``shadowBlur`` halo, then
  stroked white (2 px) while the colored shadow is still active, so the rim
  casts a full-alpha colored halo too.
- ``"new"``: contract section 2.1, mirroring ``drawProjectorScene``. Aligned
  (a camera -> projector matrix): neutral rim along the polygon offset
  outward by 6 px (3 px wide, miter limit 2), optional role fill at alpha
  .35 inset by 5 px. Not aligned: neutral rims through the contain
  transform, never fills, plus the ``Sin alinear`` hint. Ball, aim line and
  texts are neutral.
- ``"neutral"``: contract 2.2, the theme background only.

``render_pattern`` draws the coded dot frames of contract 1.2.

Canvas semantics: shapes get exact area coverage (exact along x, 8 sub-rows
along y) like a browser canvas's analytic anti-aliasing, and are blended
source-over in 8-bit sRGB. ``cv2.fillPoly`` is not used: it also paints the
pixels its edges touch, which makes every shape 0.4 px fatter per edge at 4x
supersampling. A canvas shadow is a Gaussian of sigma ``shadowBlur / 2``
(HTML spec).

Pixel convention: both projector and camera images put pixel centers at
integer coordinates. A browser canvas puts them at +0.5; that constant offset
is absorbed by the alignment because its dots are drawn the same way, so the
true camera -> projector matrix here is exactly ``inverse(G)``.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any, Callable, Mapping, NamedTuple, Sequence

import cv2
import numpy as np

ROLES = ("start", "goal", "bumper", "marker")  # PALETTE_ROLES draw order

# Contract section 2.1 (live-view.mjs constants)
RIM_OFFSET_PX = 6.0
# Light that fills a paper when its role color is not allowed: neutral, so it
# can never be taken for a post-it; none under the light theme, whose
# background already lights the wall (contract 2.1).
NEUTRAL_FILL = {"dark": "#ffffff", "light": None}
FILL_INSET_PX = 5.0
RIM_CLEAR_PX = 3.0   # a rim stays this far off every other detected note
RIM_WIDTH_PX = 3.0
RIM_MITER_LIMIT = 2.0
FILL_ALPHA = 0.35
FILL_MIN_AREA_PX2 = 25.0
BACKGROUND = {"dark": "#000000", "light": "#f3f2ec"}
RIM_COLOR = {"dark": "#ffffff", "light": "#2b2f2e"}
NEUTRAL_COLOR = {"dark": "#f6fbfc", "light": "#17211f"}  # ball, aim line, texts
UNALIGNED_HINT = "Sin alinear · pulsá A en el panel"
HINT_FONT_PX = 16
HINT_MARGIN_PX = 16
RESULT_TEXT = {"won": "¡LLEGASTE!", "lost": "FUERA DEL CAMPO"}
RESULT_FONT_PX = 42
RESULT_BASELINE_PX = 75

# Contract 1.2
PATTERN_BACKGROUND = "#000000"
PATTERN_DOT = "#ffffff"
ALIGNMENT_BITS = 7
WARMUP_FRAME = {"bit": 0, "inverse": False}

# The projector before the contract
OLD_FILL_ALPHA = 0.3
OLD_SHADOW_BLUR = 18
OLD_RIM_WIDTH_PX = 2.0
OLD_RIM_COLOR = "#ffffff"
OLD_RESULT_COLOR = {"dark": {"won": "#77ec8f", "lost": "#ff8680"},
                    "light": {"won": "#1f7a43", "lost": "#b0392e"}}
CANVAS_MITER_LIMIT = 10.0  # canvas default
FALLBACK_COLORS = {"start": "#ffdf53", "goal": "#ffa659", "bumper": "#77ec8f", "marker": "#ff77d3"}
BALL_RADIUS_PX = 10.0
AIM_LENGTH_PX = 70.0
AIM_WIDTH_PX = 3.0

SUBROWS = 8  # coverage samples per pixel row; exact along x
_EPS = 1e-9


def hex_rgb(value: str) -> tuple[int, int, int]:
    text = value.strip().lstrip("#")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _valid_hex(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 7 and value[0] == "#"
            and all(c in "0123456789abcdefABCDEF" for c in value[1:]))


def contain_transform(box_width: float, box_height: float,
                      frame_width: float, frame_height: float) -> tuple[float, float, float]:
    """``containTransform`` of ui-core.mjs: (scale, offset_x, offset_y)."""
    scale = min(box_width / frame_width, box_height / frame_height)
    return (scale, (box_width - frame_width * scale) / 2, (box_height - frame_height * scale) / 2)


# --- Geometry, ported from ui-core.mjs ---------------------------------------

def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def signed_area(points: np.ndarray) -> float:
    """Shoelace area; positive when the vertices turn like (0,0),(1,0),(1,1)."""
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def polygon_area(points) -> float:
    return abs(signed_area(np.asarray(points, dtype=np.float64).reshape(-1, 2)))


def _simple_polygon(points: np.ndarray) -> list[np.ndarray]:
    """``simplePolygon``: drop repeated points and straight-through vertices."""
    kept: list[np.ndarray] = []
    for point in points:
        if not kept or math.hypot(*(point - kept[-1])) > _EPS:
            kept.append(np.array(point, dtype=np.float64))
    while len(kept) > 1 and math.hypot(*(kept[0] - kept[-1])) <= _EPS:
        kept.pop()
    changed = True
    while changed and len(kept) >= 3:
        changed = False
        for index in range(len(kept)):
            prev, point, nxt = kept[index - 1], kept[index], kept[(index + 1) % len(kept)]
            in_x, in_y = point - prev
            out_x, out_y = nxt - point
            straight = (abs(_cross(in_x, in_y, out_x, out_y))
                        <= _EPS * math.hypot(in_x, in_y) * math.hypot(out_x, out_y))
            if straight and in_x * out_x + in_y * out_y > 0:
                del kept[index]
                changed = True
                break
    return kept


def _convex_with_sign(points: Sequence[np.ndarray], sign: float) -> bool:
    """``convexWithSign``: every turn on ``sign``'s side, once around."""
    turn = 0.0
    count = len(points)
    for index in range(count):
        prev, point, nxt = points[index - 1], points[index], points[(index + 1) % count]
        in_x, in_y = point - prev
        out_x, out_y = nxt - point
        bend = _cross(in_x, in_y, out_x, out_y)
        if np.sign(bend) != sign:
            return False
        turn += math.atan2(bend, in_x * out_x + in_y * out_y)
    return abs(turn - sign * 2 * math.pi) < 1e-6


def is_convex(points) -> bool:
    polygon = _simple_polygon(np.asarray(points, dtype=np.float64).reshape(-1, 2))
    if len(polygon) < 3:
        return False
    area = signed_area(np.asarray(polygon))
    return abs(area) > _EPS and _convex_with_sign(polygon, math.copysign(1.0, area))


def offset_polygon(points, distance: float, miter_limit: float = math.inf) -> np.ndarray | None:
    """``offsetConvexPolygon`` of ui-core.mjs, line for line.

    Every edge of a convex polygon moves ``distance`` along its normal
    (positive outward, negative inward, any winding). Vertices are the miter
    intersections of neighboring moved edges. None when the input is not
    convex or the result degenerates (an edge collapsed or reversed,
    orientation flipped, area vanished).

    ``miter_limit`` works like a canvas stroke's: an outward corner whose
    miter reaches farther than ``miter_limit * distance`` from its vertex is
    beveled into the two shifted edge ends. An inward offset never bevels.
    """
    array = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if not np.isfinite(array).all() or not math.isfinite(distance) or not miter_limit > 0:
        return None
    polygon = _simple_polygon(array)
    if len(polygon) < 3:
        return None
    area = signed_area(np.asarray(polygon))
    if not abs(area) > _EPS:
        return None
    sign = math.copysign(1.0, area)
    if not _convex_with_sign(polygon, sign):
        return None
    count = len(polygon)
    lines = []
    for index, point in enumerate(polygon):
        dx, dy = polygon[(index + 1) % count] - point
        length = math.hypot(dx, dy)
        lines.append((point[0] + sign * dy / length * distance,
                      point[1] - sign * dx / length * distance, dx, dy))
    result = []
    for index, (x, y, dx, dy) in enumerate(lines):
        px, py, pdx, pdy = lines[index - 1]
        denominator = _cross(pdx, pdy, dx, dy)
        if denominator == 0:
            return None
        along = _cross(x - px, y - py, dx, dy) / denominator
        result.append(np.array((px + along * pdx, py + along * pdy)))
    if not np.isfinite(np.asarray(result)).all():
        return None
    for index in range(count):
        step = result[(index + 1) % count] - result[index]
        if not step[0] * lines[index][2] + step[1] * lines[index][3] > _EPS:
            return None
    result_area = signed_area(np.asarray(result))
    if np.sign(result_area) != sign or not abs(result_area) > _EPS:
        return None
    if not _convex_with_sign(result, sign):
        return None
    if not distance > 0 or miter_limit == math.inf:
        return np.asarray(result)
    beveled: list[np.ndarray] = []
    for index, corner in enumerate(result):
        vertex = polygon[index]
        if math.hypot(*(corner - vertex)) <= miter_limit * distance:
            beveled.append(corner)
            continue
        # Where the previous moved edge ends and where the next one starts.
        px, py, _, _ = lines[index - 1]
        start = polygon[index - 1]
        beveled.append(vertex + (px - start[0], py - start[1]))
        beveled.append(np.array(lines[index][:2]))
    return np.asarray(beveled)


def convex_hull(points) -> np.ndarray | None:
    """``convexHull`` (Andrew's monotone chain, collinear points dropped)."""
    ordered = sorted((float(x), float(y)) for x, y in np.asarray(points, np.float64).reshape(-1, 2))

    def chain(items):
        kept: list[tuple[float, float]] = []
        for point in items:
            while len(kept) >= 2:
                (ax, ay), (bx, by) = kept[-2], kept[-1]
                if _cross(bx - ax, by - ay, point[0] - ax, point[1] - ay) > 0:
                    break
                kept.pop()
            kept.append(point)
        kept.pop()  # the last point starts the other chain
        return kept

    hull = chain(ordered) + chain(ordered[::-1])
    return np.asarray(hull) if len(hull) >= 3 else None


def rim_polygon(points) -> np.ndarray | None:
    """``rimPolygon``: the note moved RIM_OFFSET_PX outward (miter limit 2),
    around its convex hull when the note itself is not convex."""
    rim = offset_polygon(points, RIM_OFFSET_PX, RIM_MITER_LIMIT)
    if rim is None:
        hull = convex_hull(points)
        rim = offset_polygon(hull, RIM_OFFSET_PX, RIM_MITER_LIMIT) if hull is not None else None
    return rim


def fill_polygon(points) -> np.ndarray | None:
    """``fillPolygon``: FILL_INSET_PX inside the note, or None when it degenerates."""
    inset = offset_polygon(points, -FILL_INSET_PX)
    return inset if inset is not None and polygon_area(inset) >= FILL_MIN_AREA_PX2 else None


def camera_to_projector(matrix: np.ndarray, camera_size=None):
    """``cameraToProjector``: map points through H, only on the side of H's
    horizon line (w = 0) that the camera frame's center (or origin) is on."""
    matrix = np.asarray(matrix, dtype=np.float64)
    g, h, i = matrix[2]
    if camera_size is not None and all(_positive(v) for v in camera_size):
        reference = g * camera_size[0] / 2 + h * camera_size[1] / 2 + i
    else:
        reference = i
    side = math.copysign(1.0, reference) if reference != 0 else 1.0

    def mapped(point) -> np.ndarray | None:
        x, y = float(point[0]), float(point[1])
        w = g * x + h * y + i
        if not (math.isfinite(x) and math.isfinite(y)) or not side * w > _EPS:
            return None
        result = np.array(((matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]) / w,
                           (matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]) / w))
        return result if np.isfinite(result).all() else None
    return mapped


def homography_point(matrix: np.ndarray, point) -> np.ndarray | None:
    """``applyHomography``: one point, None where it maps to infinity."""
    matrix = np.asarray(matrix, dtype=np.float64)
    x, y = float(point[0]), float(point[1])
    w = matrix[2, 0] * x + matrix[2, 1] * y + matrix[2, 2]
    if not math.isfinite(w) or abs(w) < 1e-12:
        return None
    mapped = np.array(((matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]) / w,
                       (matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]) / w))
    return mapped if np.isfinite(mapped).all() else None


def homography_local_scale(matrix: np.ndarray, point) -> float | None:
    """``homographyLocalScale``: sqrt(|det J|) of the homography at ``point``."""
    mapped = homography_point(matrix, point)
    if mapped is None:
        return None
    (a, b, _), (d, e, _), (g, h, i) = matrix
    w = g * point[0] + h * point[1] + i
    jacobian = _cross((a - mapped[0] * g) / w, (d - mapped[1] * g) / w,
                      (b - mapped[0] * h) / w, (e - mapped[1] * h) / w)
    scale = math.sqrt(abs(jacobian))
    return scale if math.isfinite(scale) else None


# --- Coverage -----------------------------------------------------------------

def _subrow_centers(y0: int, y1: int) -> np.ndarray:
    offsets = (np.arange(SUBROWS) + 0.5) / SUBROWS - 0.5
    return (np.arange(y0, y1)[:, None] + offsets[None, :]).ravel()


def _span_coverage(low: np.ndarray, high: np.ndarray, x0: int, x1: int, rows: int) -> np.ndarray:
    """Mean over sub-rows of each pixel's overlap with the [low, high] span."""
    left = np.arange(x0, x1, dtype=np.float64) - 0.5
    overlap = np.minimum(high[:, None], left + 1.0) - np.maximum(low[:, None], left)
    np.clip(overlap, 0.0, 1.0, out=overlap)
    return overlap.reshape(rows, SUBROWS, x1 - x0).mean(axis=1)


def _convex_spans(points: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The x interval of a convex polygon on each horizontal line ``ys``."""
    low = np.full(ys.shape, -np.inf)
    high = np.full(ys.shape, np.inf)
    sign = math.copysign(1.0, signed_area(points))
    for index, (px, py) in enumerate(points):
        qx, qy = points[(index + 1) % len(points)]
        # Inside: sign * ((qx - px) * (y - py) - (qy - py) * (x - px)) >= 0, i.e. a * x + b >= 0.
        a = -sign * (qy - py)
        b = sign * ((qx - px) * (ys - py) + (qy - py) * px)
        if a > 0:
            np.maximum(low, -b / a, out=low)
        elif a < 0:
            np.minimum(high, -b / a, out=high)
        else:
            high[b < 0] = -np.inf
    return low, high


def convex_coverage(points, roi: tuple[int, int, int, int]) -> np.ndarray:
    """Area coverage of a convex polygon over the ROI's pixels (x0, y0, x1, y1).

    Pixel ``(i, j)`` covers ``[i - .5, i + .5] x [j - .5, j + .5]``. Exact
    along x, ``SUBROWS`` samples along y: unbiased, unlike a fill rule.
    """
    x0, y0, x1, y1 = roi
    vertices = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(vertices) < 3 or not abs(signed_area(vertices)) > _EPS:
        return np.zeros((y1 - y0, x1 - x0), np.float32)
    low, high = _convex_spans(vertices, _subrow_centers(y0, y1))
    return _span_coverage(low, high, x0, x1, y1 - y0).astype(np.float32)


def circle_coverage(center, radius: float, roi: tuple[int, int, int, int]) -> np.ndarray:
    x0, y0, x1, y1 = roi
    ys = _subrow_centers(y0, y1)
    reach = radius * radius - (ys - center[1]) ** 2
    half = np.sqrt(np.maximum(reach, 0.0))
    low = np.where(reach > 0, center[0] - half, np.inf)
    high = np.where(reach > 0, center[0] + half, -np.inf)
    return _span_coverage(low, high, x0, x1, y1 - y0).astype(np.float32)


def _ascii(text: str) -> str:
    """Hershey fonts are ASCII only; the hint keeps its length and position."""
    text = text.replace("·", "-")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


class Canvas:
    """An 8-bit RGB canvas with source-over blending of anti-aliased shapes."""

    _backgrounds: dict[tuple[int, int, str], np.ndarray] = {}

    def __init__(self, width: int, height: int, background: str):
        self.width, self.height = int(width), int(height)
        key = (self.width, self.height, background)
        if key not in Canvas._backgrounds:  # a plain copy is ~10x faster than a broadcast fill
            Canvas._backgrounds[key] = np.tile(np.asarray(hex_rgb(background), np.uint8),
                                               (self.height, self.width, 1))
        self.image = Canvas._backgrounds[key].copy()

    def _roi(self, points: np.ndarray, margin: float) -> tuple[int, int, int, int] | None:
        x0 = max(0, int(math.floor(points[:, 0].min() - margin)) - 1)
        y0 = max(0, int(math.floor(points[:, 1].min() - margin)) - 1)
        x1 = min(self.width, int(math.ceil(points[:, 0].max() + margin)) + 2)
        y1 = min(self.height, int(math.ceil(points[:, 1].max() + margin)) + 2)
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    def _blend(self, roi, coverage: np.ndarray, color: str, alpha: float) -> None:
        x0, y0, x1, y1 = roi
        region = self.image[y0:y1, x0:x1].astype(np.float32)
        weight = (np.float32(alpha) * coverage)[..., None]
        region += (np.asarray(hex_rgb(color), np.float32) - region) * weight
        self.image[y0:y1, x0:x1] = np.clip(np.rint(region), 0, 255).astype(np.uint8)

    def _shape(self, outer: np.ndarray, hole: np.ndarray | None, color: str, alpha: float,
               shadow: tuple[str, float] | None, exclude=()) -> None:
        if not np.isfinite(outer).all() or (hole is not None and not np.isfinite(hole).all()):
            return
        sigma = shadow[1] / 2 if shadow else 0.0
        roi = self._roi(outer, math.ceil(4 * sigma))
        if roi is None:
            return
        coverage = convex_coverage(outer, roi)
        if hole is not None:
            coverage = np.clip(coverage - convex_coverage(hole, roi), 0.0, 1.0)
        for region in exclude:   # a clip path: nothing lands inside these polygons
            coverage = coverage * (1.0 - np.clip(convex_coverage(region, roi), 0.0, 1.0))
        if shadow is not None and sigma > 0:
            # Canvas draws the blurred shadow first, then the shape over it.
            blurred = cv2.GaussianBlur(coverage, (0, 0), sigma, borderType=cv2.BORDER_CONSTANT)
            self._blend(roi, blurred, shadow[0], alpha)
        self._blend(roi, coverage, color, alpha)

    def fill(self, polygon, color: str, alpha: float = 1.0,
             shadow: tuple[str, float] | None = None) -> None:
        """Fill a convex polygon (every shape these renderers fill is convex)."""
        vertices = _simple_polygon(np.asarray(polygon, np.float64).reshape(-1, 2))
        if len(vertices) >= 3:
            self._shape(np.asarray(vertices), None, color, alpha, shadow)

    def stroke(self, polygon, width: float, color: str, *,
               miter_limit: float = CANVAS_MITER_LIMIT, alpha: float = 1.0,
               shadow: tuple[str, float] | None = None, exclude=()) -> None:
        """Closed-path stroke of a convex polygon, centered on it, miter joins."""
        if not is_convex(polygon):
            polygon = convex_hull(polygon)  # every stroked shape here is convex anyway
            if polygon is None:
                return
        outer = offset_polygon(polygon, width / 2, miter_limit)
        if outer is None:
            return
        # Thinner than the line: the stroke covers the whole inside.
        self._shape(outer, offset_polygon(polygon, -width / 2), color, alpha, shadow, exclude)

    def segment(self, start, end, width: float, color: str) -> None:
        """A butt-capped line, like the canvas default ``lineCap``."""
        start, end = np.asarray(start, np.float64), np.asarray(end, np.float64)
        direction = end - start
        length = float(np.linalg.norm(direction))
        if length < _EPS or not np.isfinite(direction).all():
            return
        side = np.array((-direction[1], direction[0])) / length * (width / 2)
        self._shape(np.array((start + side, end + side, end - side, start - side)), None,
                    color, 1.0, None)

    def circle(self, center, radius: float, color: str) -> None:
        center = np.asarray(center, np.float64)
        if not np.isfinite(center).all() or not radius > 0:
            return
        roi = self._roi(center.reshape(1, 2), radius)
        if roi is not None:
            self._blend(roi, circle_coverage(center, radius, roi), color, 1.0)

    def text(self, text: str, x: float, y: float, font_px: float, color: str, *,
             align: str = "left", baseline: str = "alphabetic", bold: bool = False) -> None:
        """Approximate canvas ``fillText`` with a Hershey font (glyph shapes differ)."""
        # Hershey simplex caps are ~22 px at scale 1; a UI font's are ~0.72 em.
        scale = font_px * 0.72 / 22.0
        thickness = 2 if bold else 1
        label = _ascii(text)
        (text_width, text_height), descent = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                                             scale, thickness)
        left = x - text_width / 2 if align == "center" else x
        base = y - descent if baseline == "bottom" else y
        roi = self._roi(np.array([(left, base - text_height), (left + text_width, base + descent)]), 3)
        if roi is None:
            return
        x0, y0, x1, y1 = roi
        mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.putText(mask, label, (int(round(left)) - x0, int(round(base)) - y0),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, 255, thickness, cv2.LINE_AA)
        self._blend(roi, mask.astype(np.float32) / 255.0, color, 1.0)


# --- Snapshot readers ---------------------------------------------------------

def _valid_points(shape: Any) -> np.ndarray | None:
    points = shape.get("points") if isinstance(shape, Mapping) else None
    if not isinstance(points, list) or len(points) < 3:
        return None
    try:
        array = np.array([(float(p["x"]), float(p["y"])) for p in points], dtype=np.float64)
    except (KeyError, TypeError, ValueError):
        return None
    return array if np.isfinite(array).all() else None


def _positive(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _fit(frame_width: Any, frame_height: Any, width: int, height: int):
    """``fitFrame``: the contain transform, or None for a missing size."""
    if not all(_positive(v) for v in (frame_width, frame_height, width, height)):
        return None
    return contain_transform(width, height, frame_width, frame_height)


def _notes(preview: Any):
    roles = preview.get("roles") if isinstance(preview, Mapping) else None
    if not isinstance(roles, Mapping):
        return
    for role in ROLES:
        entry = roles.get(role)
        notes = entry.get("notes") if isinstance(entry, Mapping) else None
        for shape in notes if isinstance(notes, list) else ():
            points = _valid_points(shape)
            if points is not None:
                yield role, points


def _role_hex(palette: Any, role: str, *, strict: bool) -> str:
    try:
        value = palette["roles"][role]["hex"]
    except (KeyError, TypeError):
        value = None
    if strict:  # drawProjectorScene accepts only #rrggbb
        return value if _valid_hex(value) else FALLBACK_COLORS[role]
    return value or FALLBACK_COLORS[role]  # drawLiveDetection: any truthy value


def aligned_matrix(state: Any, width: int, height: int) -> np.ndarray | None:
    """``alignedMatrix`` plus ``previewFitsAlignment`` of live-view.mjs.

    The camera -> projector matrix from ``state["alignment"]`` when it is
    ready, measured for this viewport and for the live preview's frame size.
    """
    alignment = state.get("alignment") if isinstance(state, Mapping) else None
    if not isinstance(alignment, Mapping) or alignment.get("status") != "ready":
        return None
    try:
        matrix = np.asarray(alignment.get("matrix"), dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all() or np.linalg.det(matrix) == 0:
        return None
    projector = alignment.get("projector")
    if not isinstance(projector, Mapping) or (projector.get("width"), projector.get("height")) != (width, height):
        return None
    camera = alignment.get("camera")
    preview = state.get("live_detection")
    if (isinstance(preview, Mapping) and isinstance(camera, Mapping)
            and _positive(camera.get("width")) and _positive(camera.get("height"))
            and (preview.get("width"), preview.get("height")) != (camera["width"], camera["height"])):
        return None
    return matrix


class _Mapping(NamedTuple):
    matrix: np.ndarray
    to_projector: Callable[[Any], np.ndarray | None]


def _projected_notes(preview: Any, mapping: _Mapping | None, width: int, height: int):
    """``projectedNotes``: every valid note in projector px with its role."""
    fit = _fit(preview.get("width"), preview.get("height"), width, height) \
        if isinstance(preview, Mapping) else None
    if fit is None:
        return
    scale, offset_x, offset_y = fit
    for role, points in _notes(preview):
        if mapping is None:
            yield role, points * scale + (offset_x, offset_y)
            continue
        mapped = [mapping.to_projector(point) for point in points]
        if all(point is not None for point in mapped):
            yield role, np.asarray(mapped)


def _place_ball(game: Mapping, camera: Mapping, mapping: _Mapping | None, width: int, height: int):
    """``placeBall``: center, radius and aim tip in projector px, or None."""
    try:
        position = np.array((float(game["position"]["x"]), float(game["position"]["y"])))
    except (KeyError, TypeError, ValueError):
        return None
    if not np.isfinite(position).all():
        return None
    try:
        angle = math.radians(float(game.get("aim_degrees", -45.0)))
    except (TypeError, ValueError):
        angle = math.radians(-45.0)
    direction = np.array((math.cos(angle), math.sin(angle)))
    projector_space = game.get("space") == "projector"
    if not projector_space and mapping is not None:
        center = mapping.to_projector(position)
        stretch = homography_local_scale(mapping.matrix, position) if center is not None else None
        if center is None or not _positive(stretch):
            return None
        return center, BALL_RADIUS_PX * stretch, mapping.to_projector(position + AIM_LENGTH_PX * direction)
    if projector_space:  # a game measured on another viewport is contain-fitted
        fit = _fit(game.get("width") or width, game.get("height") or height, width, height)
    else:
        fit = _fit(game.get("width") or camera.get("width"), game.get("height") or camera.get("height"),
                   width, height)
    if fit is None:
        return None
    scale, offset_x, offset_y = fit
    center = position * scale + (offset_x, offset_y)
    return center, BALL_RADIUS_PX * scale, center + AIM_LENGTH_PX * scale * direction


def _draw_game(canvas: Canvas, state: Mapping, mapping: _Mapping | None,
               ball_color: str, result_colors: Mapping[str, str]) -> None:
    game = state.get("game")
    if not isinstance(game, Mapping):
        return
    camera = state.get("camera") if isinstance(state.get("camera"), Mapping) else {}
    ball = _place_ball(game, camera, mapping, canvas.width, canvas.height)
    if ball is not None:
        center, radius, tip = ball
        if game.get("status") == "ready" and tip is not None:
            canvas.segment(center, tip, AIM_WIDTH_PX, ball_color)
        canvas.circle(center, radius, ball_color)
    status = game.get("status")
    if status in RESULT_TEXT:
        canvas.text(RESULT_TEXT[status], canvas.width / 2, RESULT_BASELINE_PX, RESULT_FONT_PX,
                    result_colors[status], align="center", bold=True)


def _draw_old(canvas: Canvas, state: Mapping, theme: str) -> None:
    """``drawLiveDetection`` then the old ``drawLiveGame`` (commit 8238278)."""
    preview = state.get("live_detection")
    fit = _fit(preview.get("width"), preview.get("height"), canvas.width, canvas.height) \
        if isinstance(preview, Mapping) else None
    if fit is not None:
        scale, offset_x, offset_y = fit
        for role, points in _notes(preview):
            color = _role_hex(state.get("palette"), role, strict=False)
            if not _valid_hex(color):
                color = FALLBACK_COLORS[role]  # a canvas ignores an unparsable color
            shape = points * scale + (offset_x, offset_y)
            halo = (color, OLD_SHADOW_BLUR)
            canvas.fill(shape, color, OLD_FILL_ALPHA, shadow=halo)
            # The white rim still casts the role-colored shadow, now at full alpha.
            canvas.stroke(shape, OLD_RIM_WIDTH_PX, OLD_RIM_COLOR, shadow=halo)
    # The old drawLiveGame knew no game.space: always camera px through contain.
    game = state.get("game")
    if isinstance(game, Mapping) and game.get("space") == "projector":
        state = {**state, "game": {**game, "space": "camera"}}
    _draw_game(canvas, state, None, NEUTRAL_COLOR[theme], OLD_RESULT_COLOR[theme])


def _draw_new(canvas: Canvas, state: Mapping, theme: str, mapping: _Mapping | None,
              fill: bool, fresh: bool, probes: bool = False) -> None:
    """``drawProjectorScene`` (contract 2.1), and the overlay check's probes (2.3)."""
    rim = RIM_COLOR[theme]
    preview = state.get("live_detection") if fresh else None
    notes = list(_projected_notes(preview, mapping, canvas.width, canvas.height))
    # Contract 2.1: a rim never lights another detected note (grown by
    # RIM_CLEAR_PX); a paper our rim would cross stays whole.
    keep_clear = [offset_polygon(shape, RIM_CLEAR_PX) if is_convex(shape) else None for _, shape in notes]
    for index, (role, shape) in enumerate(notes):
        # Aligned: role color inside the paper once the overlay check allowed
        # it; otherwise neutral light there in the dark theme (contract 2.1),
        # so a dark room still sees the whole paper and a ghost stays gray.
        color = (_role_hex(state.get("palette"), role, strict=True) if fill
                 else NEUTRAL_FILL.get(theme) if mapping is not None else None)
        if color is not None:
            inner = fill_polygon(shape)
            if inner is not None:
                canvas.fill(inner, color, FILL_ALPHA)
        outline = rim_polygon(shape)
        if outline is not None:
            canvas.stroke(outline, RIM_WIDTH_PX, rim, miter_limit=RIM_MITER_LIMIT,
                          exclude=[region for k, region in enumerate(keep_clear) if k != index and region is not None])
    if probes and fill:
        alignment = state.get("alignment") if isinstance(state.get("alignment"), Mapping) else {}
        overlay = alignment.get("overlay") if isinstance(alignment.get("overlay"), Mapping) else {}
        for probe in overlay.get("probes") or []:
            square = np.array([(point["x"], point["y"]) for point in probe["points"]], dtype=np.float64)
            canvas.fill(square, _role_hex(state.get("palette"), probe["role"], strict=True), FILL_ALPHA)
    neutral = NEUTRAL_COLOR[theme]
    _draw_game(canvas, state, mapping, neutral, {"won": neutral, "lost": neutral})
    if mapping is None:
        canvas.text(UNALIGNED_HINT, HINT_MARGIN_PX, canvas.height - HINT_MARGIN_PX, HINT_FONT_PX,
                    rim, baseline="bottom")


def render_scene(state: Mapping, theme: str, size: tuple[int, int], rules: str, *,
                 matrix: np.ndarray | None = None, fill: bool | None = None,
                 force_fill: bool = False, fresh: bool = True) -> np.ndarray:
    """uint8 RGB (height, width, 3) projector frame for one controller snapshot.

    ``state`` is a ``GameController`` snapshot (``live_detection``,
    ``palette``, ``game``, ``camera``, optionally ``alignment``).

    New rules: ``matrix`` (camera -> projector) overrides the state; without
    it the matrix comes from ``state["alignment"]`` like the projector window
    reads it. ``fill`` overrides ``alignment.overlay.fill``; ``force_fill`` is
    the overlay check's ``forceFill``. A fill is never drawn unaligned.
    ``fresh=False`` is a stale preview: no notes. The old rules ignore all of
    these; ``"neutral"`` draws the theme background only.
    """
    if theme not in BACKGROUND:
        raise ValueError("theme must be 'dark' or 'light'")
    width, height = size
    canvas = Canvas(width, height, BACKGROUND[theme])
    if rules == "old":
        _draw_old(canvas, state, theme)
    elif rules == "new":
        alignment = state.get("alignment") if isinstance(state.get("alignment"), Mapping) else {}
        if matrix is not None:
            matrix = np.asarray(matrix, dtype=np.float64)
            matrix = matrix / matrix[2, 2]
            preview = state.get("live_detection")
            camera = ((preview.get("width"), preview.get("height"))
                      if isinstance(preview, Mapping) else None)
        else:
            matrix = aligned_matrix(state, width, height)
            camera = alignment.get("camera")
            camera = (camera.get("width"), camera.get("height")) if isinstance(camera, Mapping) else None
        if fill is None:
            overlay = alignment.get("overlay")
            fill = isinstance(overlay, Mapping) and overlay.get("fill") is True
        mapping = _Mapping(matrix, camera_to_projector(matrix, camera)) if matrix is not None else None
        _draw_new(canvas, state, theme, mapping, mapping is not None and (fill or force_fill), fresh,
                  probes=force_fill)
    elif rules != "neutral":
        raise ValueError("rules must be 'old', 'new' or 'neutral'")
    return canvas.image


def alignment_code(dot_id: int) -> int:
    """Contract 1.1: 6 data bits plus an even parity bit 6."""
    if not isinstance(dot_id, int) or not 1 <= dot_id <= 63:
        raise ValueError(f"dot id out of range: {dot_id!r}")
    return dot_id | (bin(dot_id).count("1") % 2) << 6


def render_pattern(layout: Mapping, frame: Mapping | str, size: tuple[int, int] | None = None) -> np.ndarray:
    """uint8 RGB projector frame of contract 1.2 (``drawAlignmentPattern``).

    ``frame`` is ``{"bit": b, "inverse": bool}`` or ``"warmup"``. Pure black
    in every theme; dot ``id`` is a white disc of ``layout["radius"]`` iff
    ``((code(id) >> bit) & 1) == (0 if inverse else 1)``.
    """
    width, height = size or (int(layout["width"]), int(layout["height"]))
    canvas = Canvas(width, height, PATTERN_BACKGROUND)
    chosen = WARMUP_FRAME if frame == "warmup" else frame
    bit = chosen.get("bit") if isinstance(chosen, Mapping) else None
    inverse = chosen.get("inverse") if isinstance(chosen, Mapping) else None
    if not (isinstance(bit, int) and 0 <= bit < ALIGNMENT_BITS and isinstance(inverse, bool)):
        return canvas.image
    for dot in layout.get("dots", ()):
        if ((alignment_code(dot["id"]) >> bit) & 1) == (0 if inverse else 1):
            canvas.circle((dot["x"], dot["y"]), float(layout["radius"]), PATTERN_DOT)
    return canvas.image
