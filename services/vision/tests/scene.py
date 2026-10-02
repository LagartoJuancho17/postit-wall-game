"""Independent, reproducible image generator for tests and vision evals."""

import cv2
import numpy as np


WIDTH = 960
HEIGHT = 720
MARKERS = ((90, 90), (870, 100), (850, 625), (100, 615))
START = (175, 330)
GOAL = (775, 355)
GREENS = ((375, 220), (580, 460))


def bgr(hue: int) -> tuple[int, int, int]:
    pixel = np.uint8([[[hue, 235, 235]]])
    return tuple(int(value) for value in cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0])


def draw_note(
    image: np.ndarray,
    center: tuple[int, int],
    hue: int,
    size: tuple[int, int] = (55, 55),
    angle: float = 0,
) -> None:
    corners = cv2.boxPoints((center, size, angle)).round().astype(np.int32)
    cv2.fillConvexPoly(image, corners, bgr(hue))


def scene(
    *,
    brightness: float = 1.0,
    perspective: bool = False,
    speckles: bool = False,
    greens: int = 2,
) -> tuple[np.ndarray, dict[str, object]]:
    image = np.full((HEIGHT, WIDTH, 3), 225, np.uint8)
    for index, center in enumerate(MARKERS):
        draw_note(image, center, 157, (45, 45), (index - 1.5) * 9)
    draw_note(image, START, 30, (62, 62), -16)
    draw_note(image, GOAL, 12, (62, 62), 22)
    for index, center in enumerate(GREENS[:greens]):
        draw_note(image, center, 65, (82, 48), (25, -31)[index])

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
            hue = (12, 30, 65, 157)[int(rng.integers(0, 4))]
            cv2.rectangle(image, (x, y), (x + 3, y + 3), bgr(hue), -1)
    return image, expected
