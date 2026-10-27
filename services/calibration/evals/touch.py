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
- one slip: a touch 25 mm off is named for a retake (touch_retake), or
  passes only when it barely bends the outline and the result is accurate;
- wrong order: a transposed square grid is refused;
- a real hand (REAL_*): the first run on Tobias's wall (2026-10-27, a
  1920 x 1080 projector, 4 x 4) scattered ~4 px per axis with 3 of 16
  touches 16-24 px off, and the first thresholds (tuned on 2 mm hands)
  threw it all away. Here placement is REAL_PLACE_MM with a REAL_SLIP share
  of slips of 15-30 mm, and the operator retakes what the solve names
  (touch_retake) up to TOUCH_MAX_ROUNDS times: how many runs end aligned, how
  many needed a retake, and how far the accepted outlines really are;
- sloppy hands (10 mm) and a camera bumped halfway: never accepted far off.
"""

from __future__ import annotations

import json
import math

import numpy as np

from contracts.model import Point
from services.calibration import transform_point
from services.calibration.touch import (TOUCH_GRID, TOUCH_MAX_ROUNDS, TOUCH_MAX_MODEL_PX, TOUCH_OUTLIER_MIN_PX, TOUCH_OUTLIER_SIGMA,
                                        solve_touch,
                                        touch_targets)

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
REAL_PROJECTOR = (1920, 1080)
REAL_IMAGE_WIDTH_M = 2.0      # ~0.96 px/mm: the real run's ~4 px scatter is ~4 mm
REAL_PLACE_MM = 4.0
REAL_SLIP = 0.15
REAL_SEEDS = 200
PASS = {"ours_p95_px": 2.0, "ours_beats_mesh": 1.0, "accept": 0.95, "accepted_true_p95_px": 3.5,
        "slip_handled": 0.9, "transposed_refused": 1.0,
        # Bounds measured 2026-10-27 with a margin; the floor of a 4 mm hand
        # with 16 touches is ~5.4 px median / ~8.6 p95 over the whole
        # projection (the corners extrapolate), whatever the solver does.
        "slips_aligned": 0.75, "slips_true_p95_px": 12.5, "clean_first_try": 0.7, "clean_true_p95_px": 8.5,
        "careful_first_try": 0.95, "careful_true_p95_px": 5.0, "sloppy_true_p95_px": 13.5, "bumped_refused": 1.0}


def _projector_ray(q: Point, projector=PROJECTOR, width_m=IMAGE_WIDTH_M) -> np.ndarray:
    focal = projector[0] / 2 / (width_m / 2 / WALL_M)
    return np.array([(q.x - projector[0] / 2) / focal, (q.y - projector[1] / 2) / focal, 1.0])


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


def _wall(q: Point, projector=PROJECTOR, width_m=IMAGE_WIDTH_M) -> np.ndarray:
    return _projector_ray(q, projector, width_m) * WALL_M


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


def _true_errors(fn, to_camera, projector=PROJECTOR, width_m=IMAGE_WIDTH_M) -> np.ndarray:
    errors = []
    for x in np.linspace(10, projector[0] - 10, 33):
        for y in np.linspace(10, projector[1] - 10, 21):
            truth = Point(float(x), float(y))
            got = fn(to_camera(_wall(truth, projector, width_m)))
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


def _hand(rng, to_camera, target: Point, place_mm: float, slip: float) -> Point:
    """One post-it put on one cross by a real hand, as the camera sees it."""
    px_per_mm = REAL_PROJECTOR[0] / (REAL_IMAGE_WIDTH_M * 1000)
    dx, dy = rng.normal(0, place_mm * px_per_mm, 2)
    if rng.random() < slip:
        angle = rng.uniform(0, 2 * math.pi)
        reach = rng.uniform(15, 30) * px_per_mm
        dx, dy = dx + reach * math.cos(angle), dy + reach * math.sin(angle)
    seen = to_camera(_wall(Point(target.x + dx, target.y + dy), REAL_PROJECTOR, REAL_IMAGE_WIDTH_M))
    nx, ny = rng.normal(0, DETECT_PX, 2)
    return Point(seen.x + nx, seen.y + ny)


def _real_run(seed: int, place_mm: float, slip: float, bump_px: float = 0.0) -> dict:
    """A whole run with retakes, as the server drives it: solve, retake the
    named touches, solve again. bump_px moves the camera's image after the
    first half of the touches (somebody knocked the tripod)."""
    to_camera = _camera(POSES["2 m abajo"], -0.02)
    cols, rows = TOUCH_GRID
    targets = touch_targets(*REAL_PROJECTOR, cols, rows)
    rng = np.random.default_rng([seed, 2026, int(place_mm * 10), int(bump_px)])

    def touch(i: int) -> Point:
        p = _hand(rng, to_camera, targets[i], place_mm, slip)
        return Point(p.x + bump_px, p.y) if i >= len(targets) // 2 else p
    touches = [touch(i) for i in range(len(targets))]
    retaken = 0
    for round_ in range(TOUCH_MAX_ROUNDS + 1):
        result = solve_touch(touches, targets, *REAL_PROJECTOR)
        if result["ok"]:
            matrix = result["matrix"]
            true = float(np.percentile(_true_errors(lambda p: transform_point(p, matrix), to_camera,
                                                    REAL_PROJECTOR, REAL_IMAGE_WIDTH_M), 95))
            return {"ok": True, "rounds": round_, "true": true, "retaken": retaken}
        if result["code"] != "touch_retake" or round_ == TOUCH_MAX_ROUNDS:
            return {"ok": False, "rounds": round_, "code": result["code"]}
        for i in result["retake"]:
            touches[i] = touch(i)
            retaken += 1
    raise AssertionError("unreachable")


def _hands(place_mm: float, slip: float, seeds: int) -> dict:
    runs = [_real_run(seed, place_mm, slip) for seed in range(seeds)]
    ok = [r for r in runs if r["ok"]]
    trues = [r["true"] for r in ok]
    return {"aligned": round(len(ok) / len(runs), 3),
            "first_try": round(sum(1 for r in ok if r["rounds"] == 0) / len(runs), 3),
            "rounds": {str(k): sum(1 for r in ok if r["rounds"] == k) for k in range(TOUCH_MAX_ROUNDS + 1)},
            "retaken_mean": round(float(np.mean([r["retaken"] for r in ok])), 2) if ok else None,
            "true_p95_px": round(float(np.percentile(trues, 95)), 3) if ok else None,
            "true_median_px": round(float(np.median(trues)), 3) if ok else None}


def _real() -> dict:
    bumped = [_real_run(seed, REAL_PLACE_MM, 0.0, bump_px=40.0) for seed in range(40)]
    sloppy = _hands(10.0, REAL_SLIP, 60)
    return {
        "setup": {"projector": list(REAL_PROJECTOR), "image_width_m": REAL_IMAGE_WIDTH_M,
                  "place_mm": REAL_PLACE_MM, "slip_share": REAL_SLIP, "rounds": TOUCH_MAX_ROUNDS,
                  "outlier_sigma": TOUCH_OUTLIER_SIGMA, "outlier_min_px": TOUCH_OUTLIER_MIN_PX,
                  "max_model_px": TOUCH_MAX_MODEL_PX},
        "hand_with_slips": _hands(REAL_PLACE_MM, REAL_SLIP, REAL_SEEDS),
        "hand_no_slips": _hands(REAL_PLACE_MM, 0.0, 100),
        "careful_hand": _hands(2.0, 0.0, 100),
        "sloppy_hand": sloppy,
        "bumped_refused": round(sum(1 for r in bumped if not r["ok"] or r["true"] <= PASS["slips_true_p95_px"])
                                / len(bumped), 3),
    }


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
                # Named for a retake, or so small a bend that it passes and
                # is still accurate (TOUCH_MAX_SHIFT_PX).
                slips_ok += ((slip["code"] == "touch_retake" and slip["retake"] == [5])
                             or (slip["ok"] and float(np.percentile(_true_errors(
                                 lambda p: transform_point(p, slip["matrix"]), to_camera), 95))
                                 <= PASS["accepted_true_p95_px"]))
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
                  "grid": list(TOUCH_GRID), "outlier_sigma": TOUCH_OUTLIER_SIGMA},
        "poses": poses,
        "ours_p95_px_flat_lens": round(ours_worst, 3),
        "ours_beats_mesh": round(beats / cases, 3),
        "accept": round(accepted / runs, 3),
        "accepted_true_p95_px": round(float(np.percentile(accepted_true, 95)), 3) if accepted_true else None,
        "slip_handled": round(slips_ok / runs, 3),
        "transposed_refused": transposed_refused / 10,
        "real_hand": _real(),
        "pass": PASS,
    }
    real = report["real_hand"]
    report["passed"] = (report["ours_p95_px_flat_lens"] <= PASS["ours_p95_px"]
                        and report["ours_beats_mesh"] >= PASS["ours_beats_mesh"]
                        and report["accept"] >= PASS["accept"]
                        and report["accepted_true_p95_px"] is not None
                        and report["accepted_true_p95_px"] <= PASS["accepted_true_p95_px"]
                        and report["slip_handled"] >= PASS["slip_handled"]
                        and report["transposed_refused"] >= PASS["transposed_refused"]
                        and real["hand_with_slips"]["aligned"] >= PASS["slips_aligned"]
                        and real["hand_with_slips"]["true_p95_px"] <= PASS["slips_true_p95_px"]
                        and real["hand_no_slips"]["first_try"] >= PASS["clean_first_try"]
                        and real["hand_no_slips"]["true_p95_px"] <= PASS["clean_true_p95_px"]
                        and real["careful_hand"]["first_try"] >= PASS["careful_first_try"]
                        and real["careful_hand"]["true_p95_px"] <= PASS["careful_true_p95_px"]
                        and (real["sloppy_hand"]["true_p95_px"] or 0.0) <= PASS["sloppy_true_p95_px"]
                        and real["bumped_refused"] >= PASS["bumped_refused"])
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)
