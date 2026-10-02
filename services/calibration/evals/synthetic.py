"""Deterministic quality eval with noisy measurements and outlier matches.

Run from the project root: python -m services.calibration.evals.synthetic
"""

import json
import math

import cv2
import numpy as np

from contracts.model import Point
from services.calibration import detect_projected_dot, estimate_homography, transform_point


def run() -> dict[str, float | str]:
    rng = np.random.default_rng(20261002)
    true_h = np.array([[1.45, 0.09, 80], [-0.04, 1.38, 35],
                       [0.0002, -0.0001, 1]], dtype=np.float64)

    def exact(point: Point) -> Point:
        projected = true_h @ np.array((point.x, point.y, 1.0))
        return Point(*(projected[:2] / projected[2]))

    camera = [Point(float(x), float(y)) for y in np.linspace(40, 440, 6)
              for x in np.linspace(40, 600, 8)]
    observed = []
    for point in camera:
        target = exact(point)
        noise = rng.normal(0, 0.35, size=2)
        observed.append(Point(target.x + float(noise[0]),
                              target.y + float(noise[1])))
    # Corrupt 6 of 48 measured matches to verify the robust fit.
    for index in (4, 11, 21, 29, 37, 44):
        observed[index] = Point(float(rng.uniform(0, 1000)),
                                float(rng.uniform(0, 750)))
    estimated = estimate_homography(camera, observed)
    holdout = [Point(float(x), float(y)) for y in np.linspace(65, 415, 5)
               for x in np.linspace(65, 575, 7)]
    holdout_errors = [math.dist((transform_point(p, estimated).x,
                                 transform_point(p, estimated).y),
                                (exact(p).x, exact(p).y)) for p in holdout]
    p95_error = float(np.percentile(holdout_errors, 95))

    dark = np.full((240, 320, 3), 55, dtype=np.uint8)
    lit = dark.copy()
    cv2.circle(lit, (173, 127), 7, (230, 230, 230), -1)
    # Sensor noise in both frames remains below the detection threshold.
    dark = np.clip(dark.astype(np.int16) + rng.integers(-3, 4, dark.shape), 0, 255).astype(np.uint8)
    lit = np.clip(lit.astype(np.int16) + rng.integers(-3, 4, lit.shape), 0, 255).astype(np.uint8)
    dot = detect_projected_dot(dark, lit)
    dot_error = math.hypot(dot.x - 173, dot.y - 127)
    result: dict[str, float | str] = {
        "eval": "synthetic_calibration",
        "holdout_p95_projector_px": round(p95_error, 4),
        "dot_center_error_camera_px": round(dot_error, 4),
        "outlier_fraction": 6 / len(camera),
    }
    assert p95_error <= 1.0, result
    assert dot_error <= 1.0, result
    return result


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
