"""Map webcam pixels into projector pixels with a planar homography.

The projector and wall must remain still after collecting correspondences.
The fuchsia markers define the camera-visible playfield; calibration also
requires at least four known projector pixels observed by the camera.
"""

from collections.abc import Sequence
import math

import cv2
import numpy as np

from contracts.model import Board, Matrix3, Point, Polygon


def _point_array(points: Sequence[Point], name: str) -> np.ndarray:
    try:
        array = np.asarray([(point.x, point.y) for point in points], dtype=np.float64).reshape(-1, 2)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite Point values") from exc
    if array.ndim != 2 or array.shape[1] != 2 or not np.isfinite(array).all():
        raise ValueError(f"{name} must contain finite Point values")
    return array


def _matrix_array(matrix: Matrix3) -> np.ndarray:
    try:
        array = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("homography must be a finite 3 × 3 matrix") from exc
    if array.shape != (3, 3) or not np.isfinite(array).all():
        raise ValueError("homography must be a finite 3 × 3 matrix")
    return array


def estimate_homography(
    camera_points: Sequence[Point], projector_points: Sequence[Point]
) -> Matrix3:
    """Estimate camera → projector mapping from matching pixel locations.

    With more than four observations, RANSAC rejects mismatched or badly
    detected dots. Its inlier threshold is three projector pixels.
    """

    source = _point_array(camera_points, "camera_points")
    target = _point_array(projector_points, "projector_points")
    if len(source) != len(target):
        raise ValueError("camera_points and projector_points must have equal length")
    if len(source) < 4:
        raise ValueError("at least four point correspondences are required")
    if len(np.unique(source, axis=0)) < 4 or len(np.unique(target, axis=0)) < 4:
        raise ValueError("at least four distinct points are required in each image")

    method = cv2.RANSAC if len(source) > 4 else 0
    try:
        matrix, inliers = cv2.findHomography(
            source, target, method=method, ransacReprojThreshold=3.0
        )
    except cv2.error as exc:
        raise ValueError("point correspondences do not define a homography") from exc
    if matrix is None or inliers is None or int(inliers.sum()) < 4:
        raise ValueError("point correspondences do not define a homography")
    matrix = _matrix_array(matrix)
    if np.linalg.matrix_rank(matrix) < 3:
        raise ValueError("point correspondences define a degenerate homography")

    # Scale does not change the mapping. Normalizing makes saved values stable.
    scale = matrix[2, 2] if abs(matrix[2, 2]) > 1e-12 else np.linalg.norm(matrix)
    matrix = matrix / scale
    return tuple(tuple(float(value) for value in row) for row in matrix)


def transform_point(point: Point, matrix: Matrix3) -> Point:
    """Transform a camera pixel; reject positions that map to infinity."""

    if not math.isfinite(point.x) or not math.isfinite(point.y):
        raise ValueError("point coordinates must be finite")
    mapped = _matrix_array(matrix) @ np.array((point.x, point.y, 1.0))
    if abs(mapped[2]) <= 1e-12:
        raise ValueError("point maps outside the finite projector plane")
    x, y = mapped[:2] / mapped[2]
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("point maps outside the finite projector plane")
    return Point(float(x), float(y))


def transform_board(board: Board, matrix: Matrix3) -> Board:
    """Map every board polygon from camera pixels to projector pixels."""

    def mapped(polygon: Polygon) -> Polygon:
        return Polygon(tuple(transform_point(point, matrix) for point in polygon.points))

    return Board(
        playfield=mapped(board.playfield),
        start=mapped(board.start),
        goal=mapped(board.goal),
        bumpers=tuple(mapped(polygon) for polygon in board.bumpers),
        markers=tuple(mapped(polygon) for polygon in board.markers),
    )


def reprojection_errors(
    camera_points: Sequence[Point],
    projector_points: Sequence[Point],
    matrix: Matrix3,
) -> tuple[float, ...]:
    """Euclidean error in projector pixels for each observed point pair."""

    source = _point_array(camera_points, "camera_points")
    target = _point_array(projector_points, "projector_points")
    if len(source) != len(target):
        raise ValueError("camera_points and projector_points must have equal length")
    errors = []
    for src, dst in zip(source, target):
        mapped = transform_point(Point(float(src[0]), float(src[1])), matrix)
        # sqrt(dx*dx + dy*dy), not hypot: the JS port gets the same bits.
        dx, dy = mapped.x - dst[0], mapped.y - dst[1]
        errors.append(math.sqrt(dx * dx + dy * dy))
    return tuple(errors)


def detect_projected_dot(dark_bgr: np.ndarray, lit_bgr: np.ndarray) -> Point:
    """Locate one white projected dot from a dark/lit frame pair.

    The camera should have fixed exposure and position for both frames. A
    brightening component covering more than 10% of the image is treated as
    an exposure change; two similarly strong components are ambiguous.
    """

    if (
        not isinstance(dark_bgr, np.ndarray)
        or not isinstance(lit_bgr, np.ndarray)
        or dark_bgr.shape != lit_bgr.shape
        or dark_bgr.ndim != 3
        or dark_bgr.shape[0] == 0
        or dark_bgr.shape[1] == 0
        or dark_bgr.shape[2] != 3
        or dark_bgr.dtype != np.uint8
        or lit_bgr.dtype != np.uint8
    ):
        raise ValueError("dark_bgr and lit_bgr must be equal-size uint8 BGR images")

    dark_gray = cv2.cvtColor(dark_bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    lit_gray = cv2.cvtColor(lit_bgr, cv2.COLOR_BGR2GRAY).astype(np.int16)
    brightening = np.maximum(lit_gray - dark_gray, 0).astype(np.uint8)
    peak = int(brightening.max())
    if peak < 20:
        raise ValueError("no projected dot detected: image did not brighten enough")

    threshold = max(20, int(peak * 0.35))
    mask = (brightening >= threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    components: list[tuple[float, int, Point]] = []
    image_area = dark_gray.shape[0] * dark_gray.shape[1]
    for label in range(1, count):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area > image_area * 0.1:
            raise ValueError("no unique projected dot: large exposure change")
        if area < 3:
            continue  # Single hot pixels should not count as a projected dot.
        left, top, width, height = (int(value) for value in stats[label, :4])
        local_ys, local_xs = np.where(
            labels[top:top + height, left:left + width] == label
        )
        ys, xs = local_ys + top, local_xs + left
        weights = brightening[ys, xs].astype(np.float64)
        strength = float(weights.sum())
        center = Point(float(np.average(xs, weights=weights)),
                       float(np.average(ys, weights=weights)))
        components.append((strength, area, center))

    if not components:
        raise ValueError("no projected dot detected")
    components.sort(key=lambda component: component[0], reverse=True)
    if len(components) > 1 and components[1][0] >= components[0][0] * 0.5:
        raise ValueError("no unique projected dot: multiple bright regions")
    return components[0][2]


# Imported last: the coded dot alignment builds on estimate_homography and
# reprojection_errors defined above.
from services.calibration.coded import (  # noqa: E402
    ALIGNMENT_FRAMES,
    ALIGNMENT_REASONS,
    alignment_code,
    alignment_layout,
    decode_alignment,
    pattern_dots,
)

__all__ = [
    "ALIGNMENT_FRAMES",
    "ALIGNMENT_REASONS",
    "alignment_code",
    "alignment_layout",
    "decode_alignment",
    "detect_projected_dot",
    "estimate_homography",
    "pattern_dots",
    "reprojection_errors",
    "transform_board",
    "transform_point",
]
