"""HSV segmentation of a stationary board while the projector is black.

The returned coordinates are camera pixels. A separate calibration service
maps them into projector pixels before the game uses them.
"""

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
    polygon: Polygon
    center: Point
    area: float


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


def _mask(hsv: np.ndarray, rule: ColorRule) -> np.ndarray:
    floor_s, floor_v = rule.min_saturation, rule.min_value
    if rule.hue_low <= rule.hue_high:
        return cv2.inRange(hsv, (rule.hue_low, floor_s, floor_v), (rule.hue_high, 255, 255))
    return (cv2.inRange(hsv, (rule.hue_low, floor_s, floor_v), (179, 255, 255))
            | cv2.inRange(hsv, (0, floor_s, floor_v), (rule.hue_high, 255, 255)))


def _find_candidates(hsv: np.ndarray, rule: ColorRule) -> list[_Candidate]:
    height, width = hsv.shape[:2]
    mask = _mask(hsv, rule)
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    # At 1080p the floor is roughly 250 px². This rejects isolated hot
    # pixels without depending on a particular webcam resolution.
    min_area = max(80.0, width * height * 0.00012)
    # A note is a small part of a wall that also shows the whole field. A
    # bigger blob is a tinted wall or a large object, never a post-it.
    max_area = width * height * MAX_NOTE_FRACTION
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[_Candidate] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
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


def _candidates_by_role(frame_bgr: np.ndarray, palette: Palette) -> dict[str, list[_Candidate]]:
    if not isinstance(frame_bgr, np.ndarray) or frame_bgr.ndim != 3:
        raise ValueError("frame_bgr debe ser una imagen BGR de tres canales")
    if frame_bgr.shape[2] != 3 or frame_bgr.dtype != np.uint8:
        raise ValueError("frame_bgr debe tener dtype uint8 y tres canales BGR")
    if min(frame_bgr.shape[:2]) < 20:
        raise ValueError("La imagen de la webcam es demasiado pequeña")
    if not isinstance(palette, Palette):
        raise TypeError("palette debe ser un contracts.palette.Palette")
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    return {role: _find_candidates(hsv, palette.rule(role)) for role in ROLES}


def find_notes(frame_bgr: np.ndarray,
               palette: Palette = DEFAULT_PALETTE) -> dict[str, tuple[Polygon, ...]]:
    """Every note of each role that passes the shape filters, largest first.

    Unlike ``detect_board`` it never requires a count, so a live preview can
    show a partial or ambiguous wall. The filters and colors are identical.
    """
    found = _candidates_by_role(frame_bgr, palette)
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
