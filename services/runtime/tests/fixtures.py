"""Synthetic physical board and known camera-to-projector mapping.

This fixture is local to the runtime service. Its drawing and mapping data
are independent of the detector and homography implementations under test.
"""

import cv2
import numpy as np

from contracts import Point


CAMERA_SIZE = (800, 600)
PROJECTOR_SIZE = (1280, 800)
MARKER_CENTERS = ((80, 75), (720, 85), (710, 520), (90, 510))
START_CENTER = (155, 315)
GOAL_CENTER = (650, 310)


def _bgr(hue: int) -> tuple[int, int, int]:
    value = cv2.cvtColor(np.uint8([[[hue, 235, 235]]]), cv2.COLOR_HSV2BGR)[0, 0]
    return tuple(int(channel) for channel in value)


def _note(
    image: np.ndarray,
    center: tuple[int, int],
    size: tuple[int, int],
    angle: float,
    hue: int,
) -> None:
    box = cv2.boxPoints((center, size, angle)).round().astype(np.int32)
    cv2.fillConvexPoly(image, box, _bgr(hue))


def camera_frame(
    *,
    green_center: tuple[int, int] = (405, 190),
    brightness: float = 1.0,
    speckles: bool = False,
) -> np.ndarray:
    width, height = CAMERA_SIZE
    image = np.full((height, width, 3), 220, np.uint8)
    for center, angle in zip(MARKER_CENTERS, (-10, 7, -8, 12)):
        _note(image, center, (40, 40), angle, 157)
    _note(image, START_CENTER, (55, 55), -13, 30)
    _note(image, GOAL_CENTER, (55, 55), 19, 12)
    _note(image, green_center, (82, 50), 28, 65)
    if brightness != 1:
        image = np.clip(image.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    if speckles:
        rng = np.random.default_rng(3127)
        for _ in range(30):
            x = int(rng.integers(0, width - 5))
            y = int(rng.integers(0, height - 5))
            cv2.rectangle(image, (x, y), (x + 2, y + 2), _bgr((12, 30, 65, 157)[_ % 4]), -1)
    return image


def known_mapping() -> np.ndarray:
    """A skewed, invertible camera → projector homography."""
    camera = np.float32([[0, 0], [799, 0], [799, 599], [0, 599]])
    projector = np.float32([[40, 22], [1230, 8], [1215, 775], [62, 760]])
    return cv2.getPerspectiveTransform(camera, projector)


def mapped(point: Point, matrix: np.ndarray) -> Point:
    result = cv2.perspectiveTransform(
        np.float32([[[point.x, point.y]]]), matrix
    )[0, 0]
    return Point(float(result[0]), float(result[1]))


def camera_observations(projector_points: tuple[Point, ...]) -> tuple[Point, ...]:
    inverse = np.linalg.inv(known_mapping())
    return tuple(mapped(point, inverse) for point in projector_points)
