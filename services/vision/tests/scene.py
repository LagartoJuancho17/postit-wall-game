"""Independent, reproducible image generator for tests and vision evals."""

import cv2
import numpy as np


WIDTH = 960
HEIGHT = 720
MARKERS = ((90, 90), (870, 100), (850, 625), (100, 615))
START = (175, 330)
GOAL = (775, 355)
GREENS = ((375, 220), (580, 460))
DEFAULT_HUES = {"start": 30, "goal": 12, "bumper": 65, "marker": 157}


def bgr(hue: int, saturation: int = 235) -> tuple[int, int, int]:
    pixel = np.uint8([[[hue, saturation, 235]]])
    return tuple(int(value) for value in cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0])


def draw_note(
    image: np.ndarray,
    center: tuple[int, int],
    hue: int,
    size: tuple[int, int] = (55, 55),
    angle: float = 0,
    saturation: int = 235,
) -> None:
    corners = cv2.boxPoints((center, size, angle)).round().astype(np.int32)
    cv2.fillConvexPoly(image, corners, bgr(hue, saturation))


def hex_at(image: np.ndarray, center: object, radius: int = 2) -> str:
    """What an operator's eyedropper reads: the mean color around a point."""
    x, y = (int(round(float(value))) for value in np.asarray(center).reshape(2))
    patch = image[y - radius:y + radius + 1, x - radius:x + radius + 1].reshape(-1, 3)
    blue, green, red = (int(round(channel)) for channel in patch.mean(axis=0))
    return f"#{red:02x}{green:02x}{blue:02x}"


def scene(
    *,
    brightness: float = 1.0,
    perspective: bool = False,
    speckles: bool = False,
    greens: int = 2,
    hues: dict[str, int] | None = None,
    saturation: int = 235,
) -> tuple[np.ndarray, dict[str, object]]:
    hue = {**DEFAULT_HUES, **(hues or {})}
    image = np.full((HEIGHT, WIDTH, 3), 225, np.uint8)
    for index, center in enumerate(MARKERS):
        draw_note(image, center, hue["marker"], (45, 45), (index - 1.5) * 9, saturation)
    draw_note(image, START, hue["start"], (62, 62), -16, saturation)
    draw_note(image, GOAL, hue["goal"], (62, 62), 22, saturation)
    for index, center in enumerate(GREENS[:greens]):
        draw_note(image, center, hue["bumper"], (82, 48), (25, -31)[index], saturation)

    expected: dict[str, object] = {
        "markers": np.asarray(MARKERS, np.float32),
        "start": np.asarray(START, np.float32),
        "goal": np.asarray(GOAL, np.float32),
        "greens": np.asarray(GREENS[:greens], np.float32).reshape(-1, 2),
    }
    if perspective:
        src = np.float32([[0, 0], [WIDTH - 1, 0], [WIDTH - 1, HEIGHT - 1], [0, HEIGHT - 1]])
        dst = np.float32([[45, 30], [WIDTH - 55, 10], [WIDTH - 20, HEIGHT - 30], [20, HEIGHT - 55]])
        matrix = cv2.getPerspectiveTransform(src, dst)
        image = cv2.warpPerspective(image, matrix, (WIDTH, HEIGHT), borderValue=(225, 225, 225))
        for key, centers in expected.items():
            points = np.asarray(centers, np.float32).reshape(-1, 1, 2)
            expected[key] = cv2.perspectiveTransform(points, matrix).reshape(-1, 2)
        expected["start"] = expected["start"][0]
        expected["goal"] = expected["goal"][0]

    if brightness != 1:
        image = np.clip(image.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    if speckles:
        rng = np.random.default_rng(211)
        for _ in range(36):
            x = int(rng.integers(0, WIDTH - 4))
            y = int(rng.integers(0, HEIGHT - 4))
            speck = (hue["goal"], hue["start"], hue["bumper"], hue["marker"])[int(rng.integers(0, 4))]
            cv2.rectangle(image, (x, y), (x + 3, y + 3), bgr(speck, saturation), -1)
    return image, expected
