"""Periodic deterministic quality eval for color and geometry detection.

Run from the project root: python -m services.vision.eval
"""

import json

import numpy as np

from services.vision import detect_board
from services.vision.tests.scene import scene


def _center(polygon) -> np.ndarray:
    return np.mean([(point.x, point.y) for point in polygon.points], axis=0)


def run_eval() -> dict[str, object]:
    cases = (
        ("normal", {}),
        ("perspective", {"perspective": True}),
        ("dim_and_speckles", {"brightness": 0.55, "speckles": True}),
        ("skew_dim_and_speckles", {"brightness": 0.7, "perspective": True, "speckles": True}),
    )
    results = []
    for name, options in cases:
        image, expected = scene(**options)
        try:
            board = detect_board(image)
            errors = [
                float(np.linalg.norm(_center(board.start) - expected["start"])),
                float(np.linalg.norm(_center(board.goal) - expected["goal"])),
            ]
            errors.extend(
                float(np.linalg.norm((point.x, point.y) - target))
                for point, target in zip(board.playfield.points, expected["markers"])
            )
            detected = len(board.markers) == 4 and len(board.bumpers) == 2
            max_error = max(errors)
        except ValueError:
            detected = False
            max_error = float("inf")
        passed = detected and max_error <= 6
        results.append({"case": name, "passed": passed, "max_center_error_px": round(max_error, 2)})
    score = sum(case["passed"] for case in results) / len(results)
    return {"score": score, "pass_threshold": 1.0, "cases": results}


if __name__ == "__main__":
    result = run_eval()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["score"] >= result["pass_threshold"] else 1)
