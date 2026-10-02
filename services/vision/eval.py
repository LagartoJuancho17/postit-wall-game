"""Periodic deterministic quality eval for color and geometry detection.

Run from the project root: python -m services.vision.eval

The "picked" cases repaint the wall with other colors and build the palette
the way an operator does: by sampling one note of each role in the image.
"""

import json

import numpy as np

from services.vision import build_palette, detect_board
from services.vision.tests.scene import hex_at, scene

RECOLORED = {"start": 105, "goal": 176, "bumper": 28, "marker": 140}


def _center(polygon) -> np.ndarray:
    return np.mean([(point.x, point.y) for point in polygon.points], axis=0)


def _sampled_palette(image, expected):
    return build_palette({
        "start": hex_at(image, expected["start"]),
        "goal": hex_at(image, expected["goal"]),
        "bumper": hex_at(image, expected["greens"][0]),
        "marker": hex_at(image, expected["markers"][0]),
    })


def run_eval() -> dict[str, object]:
    cases = (
        ("normal", {}, False),
        ("perspective", {"perspective": True}, False),
        ("dim_and_speckles", {"brightness": 0.55, "speckles": True}, False),
        ("skew_dim_and_speckles", {"brightness": 0.7, "perspective": True, "speckles": True}, False),
        ("picked_recolored_red_wrap", {"hues": RECOLORED, "speckles": True}, True),
        ("picked_recolored_skew_dim", {"hues": RECOLORED, "perspective": True,
                                       "brightness": 0.7, "speckles": True}, True),
        ("picked_pastel", {"saturation": 70, "brightness": 0.8}, True),
    )
    results = []
    for name, options, picked in cases:
        image, expected = scene(**options)
        try:
            palette = _sampled_palette(image, expected) if picked else None
            board = detect_board(image, palette) if palette else detect_board(image)
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
        results.append({"case": name, "palette": "picked" if picked else "default",
                        "passed": passed, "max_center_error_px": round(max_error, 2)})
    score = sum(case["passed"] for case in results) / len(results)
    return {"score": score, "pass_threshold": 1.0, "cases": results}


if __name__ == "__main__":
    result = run_eval()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["score"] >= result["pass_threshold"] else 1)
