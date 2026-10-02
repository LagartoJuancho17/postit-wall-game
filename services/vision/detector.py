"""HSV segmentation of a stationary board while the projector is black.

The returned coordinates are camera pixels. A separate calibration service
maps them into projector pixels before the game uses them.
"""

from dataclasses import dataclass

import cv2
import numpy as np

from contracts import Board, Point, Polygon
from contracts.geometry import quadrilateral_center


class DetectionError(ValueError):
    """The frame does not contain an unambiguous playable board."""

    def __init__(self, color: str, expected: int | str, found: int):
        self.color = color
        self.expected = expected
        self.found = found
        super().__init__(
            f"Post-its {color}: se esperaban {expected}; se detectaron {found}. "
            "Apaga la proyección, comprueba la iluminación y vuelve a escanear."
        )


@dataclass(frozen=True)
class _Candidate:
    polygon: Polygon
    center: Point
    area: float


# OpenCV HSV uses H = 0..179. Keep orange and yellow disjoint so a note
# cannot count as both. Saturation rejects the white wall and projector light.
_HUE_RANGES: dict[str, tuple[int, int]] = {
    "naranja": (5, 19),
    "amarillos": (20, 39),
    "verdes": (40, 90),
    "fucsia/violeta": (125, 179),
}
_MIN_SATURATION = 75
_MIN_VALUE = 55


def _center(polygon: Polygon) -> Point:
    return quadrilateral_center(polygon)


def _polygon_from_contour(contour: np.ndarray) -> Polygon:
    perimeter = cv2.arcLength(contour, True)
    approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
    if len(approx) == 4 and cv2.isContourConvex(approx):
        vertices = approx.reshape(4, 2).astype(np.float32)
    else:
        vertices = cv2.boxPoints(cv2.minAreaRect(contour))
    return Polygon(tuple(Point(float(x), float(y)) for x, y in vertices))


def _find_candidates(hsv: np.ndarray, color: str) -> list[_Candidate]:
    height, width = hsv.shape[:2]
    lower_hue, upper_hue = _HUE_RANGES[color]
    mask = cv2.inRange(
        hsv,
        (lower_hue, _MIN_SATURATION, _MIN_VALUE),
        (upper_hue, 255, 255),
    )
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # At 1080p the floor is roughly 250 px². This rejects isolated hot
    # pixels without depending on a particular webcam resolution.
    min_area = max(80.0, width * height * 0.00012)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[_Candidate] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        (_, _), (rect_width, rect_height), _ = cv2.minAreaRect(contour)
        short_side = min(rect_width, rect_height)
        long_side = max(rect_width, rect_height)
        if short_side < 5 or long_side / short_side > 4.0:
            continue
        if area / (rect_width * rect_height) < 0.55:
            continue
        polygon = _polygon_from_contour(contour)
        candidates.append(_Candidate(polygon, _center(polygon), area))
    return sorted(candidates, key=lambda candidate: candidate.area, reverse=True)


def _ordered_markers(markers: list[_Candidate]) -> tuple[_Candidate, ...]:
    """Order four marker centers clockwise, beginning at camera top-left."""
    centers = np.array([(m.center.x, m.center.y) for m in markers], dtype=np.float32)
    hull = cv2.convexHull(centers).reshape(-1, 2)
    if len(hull) != 4 or cv2.contourArea(hull) < 100:
        raise DetectionError("fucsia/violeta (esquinas no válidas)", 4, len(hull))
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


def detect_board(frame_bgr: np.ndarray) -> Board:
    """Return visible notes and marker centers from a projector-dark frame.

    Four fuchsia/violet markers, one yellow start and one orange goal are
    required. Green bumpers are optional. Raise ``DetectionError`` for missing
    or ambiguous required colors. ``playfield`` consists of marker centers in
    camera order: top-left, top-right, bottom-right, bottom-left.
    """
    if not isinstance(frame_bgr, np.ndarray) or frame_bgr.ndim != 3:
        raise ValueError("frame_bgr debe ser una imagen BGR de tres canales")
    if frame_bgr.shape[2] != 3 or frame_bgr.dtype != np.uint8:
        raise ValueError("frame_bgr debe tener dtype uint8 y tres canales BGR")
    if min(frame_bgr.shape[:2]) < 20:
        raise ValueError("La imagen de la webcam es demasiado pequeña")

    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    found = {color: _find_candidates(hsv, color) for color in _HUE_RANGES}
    for color, expected in (("fucsia/violeta", 4), ("amarillos", 1), ("naranja", 1)):
        if len(found[color]) != expected:
            raise DetectionError(color, expected, len(found[color]))

    ordered = _ordered_markers(found["fucsia/violeta"])
    return Board(
        playfield=Polygon(tuple(marker.center for marker in ordered)),
        start=found["amarillos"][0].polygon,
        goal=found["naranja"][0].polygon,
        bumpers=tuple(candidate.polygon for candidate in found["verdes"]),
        markers=tuple(marker.polygon for marker in ordered),
    )
