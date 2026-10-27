"""Periodic eval: the touch-grid alignment (contract 1.6) against the CCV mesh.

Run from the project root: ``python -m services.calibration.evals.touch``.
Deterministic (seeded). A physical scene in meters: the projector at the
origin throws a 2.4 m wide image on a flat wall 3 m away; the camera sits
beside or below it (Tobias, 2026-10-27: "la cámara a 2 m por debajo del
proyector") and looks at the middle of the projection, with or without a
barrel lens. A hand puts each post-it on its cross with a placement error
of PLACE_MM per axis; the camera measures its center with DETECT_PX noise.

It measures what the touch method is for and why it fits one homography
instead of CCV's triangle mesh:

- accuracy: true error (projector px) over a dense grid of the whole
  projection, ours vs. CCV's barycentric mesh (tbeta CalibrationUtils), for
  every camera pose;
- acceptance: how often a careful run passes solve_touch, and that what
  passes is really within the threshold;
- one slip: a touch 25 mm off is dropped, the run still passes;
- wrong order: a transposed square grid is refused.
"""

from __future__ import annotations

import json
import math

import numpy as np

from contracts.model import Point
from services.calibration import transform_point
from services.calibration.touch import TOUCH_GRID, TOUCH_MAX_P95_PX, solve_touch, touch_targets

PROJECTOR = (1280, 800)
CAMERA = (1280, 720)
WALL_M = 3.0
IMAGE_WIDTH_M = 2.4
PX_PER_MM = PROJECTOR[0] / (IMAGE_WIDTH_M * 1000)
PLACE_MM = 2.0
DETECT_PX = 0.3
SEEDS = 40
POSES = {   # camera position (m): x right, y down, z toward the wall
    "al lado del proyector": (0.25, 0.0, 0.0),
    "1 m abajo": (0.1, 1.0, 0.2),
    "2 m abajo": (0.1, 2.0, 0.3),
    "2 m abajo y 1 m al costado": (1.0, 2.0, 0.3),
}
LENSES = {"lente recto": 0.0, "lente con barril": -0.05}
PASS = {"ours_p95_px": 2.0, "ours_beats_mesh": 1.0, "accept": 0.95, "accepted_true_p95_px": 3.5,
        "slip_accept": 0.9, "transposed_refused": 1.0}


def _projector_ray(q: Point) -> np.ndarray:
    focal = PROJECTOR[0] / 2 / (IMAGE_WIDTH_M / 2 / WALL_M)
    return np.array([(q.x - PROJECTOR[0] / 2) / focal, (q.y - PROJECTOR[1] / 2) / focal, 1.0])


def _camera(position, k: float):
    center = np.array(position, dtype=np.float64)
    forward = np.array([0.0, 0.0, WALL_M]) - center
    forward /= np.linalg.norm(forward)
    right = np.cross(np.array([0.0, 1.0, 0.0]), forward)   # y points down: right = down x forward
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    focal = CAMERA[0] / 2 / math.tan(math.radians(35))

    def to_camera(wall: np.ndarray) -> Point:
        v = wall - center
        x, y, z = v @ right, v @ down, v @ forward
        x, y = x / z, y / z
        scale = 1 + k * (x * x + y * y)
        return Point(CAMERA[0] / 2 + focal * x * scale, CAMERA[1] / 2 + focal * y * scale)
    return to_camera


def _wall(q: Point) -> np.ndarray:
    return _projector_ray(q) * WALL_M


def _mesh_map(cameras: list[Point], targets: list[Point], cols: int, rows: int):
    """CCV's cameraToScreenSpace: barycentric in the touched triangle; outside
    the mesh (CCV returns 0,0, a FIXME) the nearest triangle extrapolates."""
    triangles = []
    for j in range(rows - 1):
        for i in range(cols - 1):
            a = i + j * cols
            triangles += [(a, a + 1, a + cols), (a + 1, a + cols + 1, a + cols)]

    def weights(p, A, B, C):
        total = (A.x - B.x) * (A.y - C.y) - (A.y - B.y) * (A.x - C.x)
        wa = ((p.x - B.x) * (p.y - C.y) - (p.y - B.y) * (p.x - C.x)) / total
        wb = ((A.x - p.x) * (A.y - C.y) - (A.y - p.y) * (A.x - C.x)) / total
        return wa, wb, 1 - wa - wb

    def mapped(p: Point) -> Point:
        best = None
        for t in triangles:
            w = weights(p, *(cameras[i] for i in t))
            if best is None or min(w) > min(best[0]):
                best = (w, t)
            if min(w) >= 0:
                break
        w, t = best
        return Point(sum(wi * targets[i].x for wi, i in zip(w, t)), sum(wi * targets[i].y for wi, i in zip(w, t)))
    return mapped


def _true_errors(fn, to_camera) -> np.ndarray:
    errors = []
    for x in np.linspace(10, PROJECTOR[0] - 10, 33):
        for y in np.linspace(10, PROJECTOR[1] - 10, 21):
            truth = Point(float(x), float(y))
            got = fn(to_camera(_wall(truth)))
            errors.append(math.hypot(got.x - truth.x, got.y - truth.y))
    return np.array(errors)


def _touches(rng, to_camera, targets, slip: int | None = None) -> list[Point]:
    touches = []
    for index, target in enumerate(targets):
        dx, dy = rng.normal(0, PLACE_MM * PX_PER_MM, 2)
        if index == slip:
            dx += 25 * PX_PER_MM
        seen = to_camera(_wall(Point(target.x + dx, target.y + dy)))
        nx, ny = rng.normal(0, DETECT_PX, 2)
        touches.append(Point(seen.x + nx, seen.y + ny))
    return touches


def run() -> dict:
    cols, rows = TOUCH_GRID
    targets = touch_targets(*PROJECTOR, cols, rows)
    poses = {}
    accepted = runs = slips_ok = 0
    accepted_true = []
    beats = cases = 0
    ours_worst = 0.0
    for pose_name, position in POSES.items():
        for lens_name, k in LENSES.items():
            to_camera = _camera(position, k)
            ours, mesh = [], []
            for seed in range(SEEDS):
                rng = np.random.default_rng([seed, len(pose_name), int(k * 100) + 50])
                touches = _touches(rng, to_camera, targets)
                result = solve_touch(touches, targets, *PROJECTOR)
                runs += 1
                if result["ok"]:
                    matrix = result["matrix"]
                    true = float(np.percentile(_true_errors(lambda p: transform_point(p, matrix), to_camera), 95))
                    ours.append(true)
                    accepted += 1
                    accepted_true.append(true)
                if seed < 10:   # the mesh is slow in Python: 10 runs per pose are enough
                    mesh.append(float(np.percentile(_true_errors(_mesh_map(touches, targets, cols, rows), to_camera), 95)))
                slip = solve_touch(_touches(np.random.default_rng([seed, 99]), to_camera, targets, slip=5),
                                   targets, *PROJECTOR)
                slips_ok += slip["ok"] and slip["dropped"] == 6
            ours_median = float(np.median(ours)) if ours else math.inf
            mesh_median = float(np.median(mesh))
            cases += 1
            beats += ours_median < mesh_median
            if lens_name == "lente recto":
                ours_worst = max(ours_worst, ours_median)
            poses[f"{pose_name}, {lens_name}"] = {"ours_true_p95_px": round(ours_median, 2),
                                                  "ccv_mesh_true_p95_px": round(mesh_median, 2),
                                                  "accepted": f"{len(ours)}/{SEEDS}"}
    transposed_refused = 0
    for seed in range(10):
        to_camera = _camera(POSES["2 m abajo"], 0.0)
        touches = _touches(np.random.default_rng([seed, 7]), to_camera, targets)
        flipped = [touches[(i % cols) * cols + i // cols] for i in range(len(touches))]
        transposed_refused += not solve_touch(flipped, targets, *PROJECTOR)["ok"]
    report = {
        "setup": {"wall_m": WALL_M, "image_width_m": IMAGE_WIDTH_M, "place_mm": PLACE_MM, "detect_px": DETECT_PX,
                  "grid": list(TOUCH_GRID), "threshold_p95_px": TOUCH_MAX_P95_PX},
        "poses": poses,
        "ours_p95_px_flat_lens": round(ours_worst, 3),
        "ours_beats_mesh": round(beats / cases, 3),
        "accept": round(accepted / runs, 3),
        "accepted_true_p95_px": round(float(np.percentile(accepted_true, 95)), 3) if accepted_true else None,
        "slip_accept": round(slips_ok / runs, 3),
        "transposed_refused": transposed_refused / 10,
        "pass": PASS,
    }
    report["passed"] = (report["ours_p95_px_flat_lens"] <= PASS["ours_p95_px"]
                        and report["ours_beats_mesh"] >= PASS["ours_beats_mesh"]
                        and report["accept"] >= PASS["accept"]
                        and report["accepted_true_p95_px"] is not None
                        and report["accepted_true_p95_px"] <= PASS["accepted_true_p95_px"]
                        and report["slip_accept"] >= PASS["slip_accept"]
                        and report["transposed_refused"] >= PASS["transposed_refused"])
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)
